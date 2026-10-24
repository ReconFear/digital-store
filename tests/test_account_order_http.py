"""PR6 этап 2e-2: POST /api/v2/orders с cookie сессии — HTTP-контракт привязки заказа к аккаунту, офлайн.

Три части:
  * accounts.order_http.AccountOrders на настоящем Starlette Request и поддельной функции создания заказа:
    перевод исходов случая B в ответы, отказ 403 до создания заказа, 422 без введённых значений;
  * AccountResponseGuard в сборке install_http_stack: no-store и JSON 500 для заказа с cookie, гостевой заказ
    без cookie не затрагивается;
  * настоящий api.py в отдельном процессе Python (как в test_http_stack.py): python-dotenv обезврежен до импорта,
    psycopg.connect падает, платежи выключены. Подменено только database.get_connection — поддельное соединение
    из test_account_order_link.py, поэтому работают настоящие create_order_with_access и commerce.orders.create_order,
    а журнал шагов показывает COMMIT или ROLLBACK. При выключенных аккаунтах в процессе любое чтение cookie
    (starlette HTTPConnection.cookies) — ошибка.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from fastapi import FastAPI, Request as FastAPIRequest
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.requests import Request

from accounts.config import AccountSettings
from accounts.order_http import AccountOrders, presents_session
from accounts.order_link import SQL_LINK_NEW_ORDER
from accounts.request_guard import OriginAllowlist, csrf_token
from accounts.session import SESSION_COOKIE_NAME, SessionToken, clear_session_cookie_header
from http_stack import install_http_stack
from test_account_http import CSRF_KEY, SETTINGS, SHOP, TRUSTED, FakeService
from test_account_web_config import _base
from test_http_stack import _secrets_file

PROJECT_DIR = Path(__file__).resolve().parent.parent
EVIL = "https://evil.example.test"
ORDER_ID = 41
GUEST_BODY = {"order": {"id": ORDER_ID, "status": "pending"}, "access_token": "t" * 43}
LINKED = {"result": "linked"}
SECRET_INPUT = "EXTRA-SECRET-VALUE"
CSRF_FAILED = {"detail": {"code": "csrf_failed", "message": "Запрос отклонён проверкой безопасности."}}
SESSION_INVALID = {"detail": {"code": "session_invalid", "message": "Нужно войти в аккаунт."}}
INVALID_REQUEST = {"detail": {"code": "invalid_request", "message": "Неверный формат запроса."}}
INTERNAL_ERROR = {"detail": {"code": "internal_error", "message": "Внутренняя ошибка."}}


# ---------- AccountOrders: поддельное создание заказа ----------

def _request(headers=(), path="/api/v2/orders", method="POST", request_class=Request):
    raw = [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in headers]
    return request_class({"type": "http", "method": method, "path": path, "headers": raw, "query_string": b""})


class _NoCookies(Request):
    @property
    def cookies(self):
        raise AssertionError("cookie прочитана")


def _session_headers(session, csrf=None, **replace):
    headers = {"Origin": SHOP, "Content-Type": "application/json",
               "Cookie": f"{SESSION_COOKIE_NAME}={session.value}",
               "X-CSRF-Token": csrf if csrf is not None else csrf_token(CSRF_KEY, session)}
    headers.update(replace)
    return [(name, value) for name, value in headers.items() if value is not None]


class _Cursor:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return (self.answer,)


class _Create:
    """Как create_order_with_access + сериализация в api.py: вызывает after_access на «курсоре транзакции»."""

    def __init__(self, answer=LINKED, error=None, body=GUEST_BODY):
        self.answer, self.error, self.body = answer, error, body
        self.cursors = []

    def __call__(self, after_access):
        cursor = _Cursor(self.answer)
        self.cursors.append(cursor)
        after_access(cursor, ORDER_ID)
        if self.error is not None:
            raise self.error
        return self.body


class _RuleError(Exception):
    """Как commerce.orders.OrderError: http_status и as_detail()."""

    http_status = 404

    def as_detail(self):
        return {"code": "product_not_found", "message": "Товар не найден или не продаётся.", "product_ids": [6]}


ORDERS = AccountOrders(SETTINGS, TRUSTED)


def _json(response):
    return json.loads(response.body)


def _no_store(response):
    return response.headers.get("cache-control") == "no-store"


def test_disabled_accounts_never_read_the_cookie():
    orders = AccountOrders(AccountSettings())                               # trusted_origins не нужен
    create = _Create()
    request = _request(_session_headers(SessionToken.new()), request_class=_NoCookies)
    assert orders.respond(request, create) is None and create.cursors == []
    error = RequestValidationError([{"type": "missing", "loc": ("body", "items"), "msg": "Field required",
                                     "input": {}}])
    response = asyncio.run(orders.validation_error(request, error))
    assert (response.status_code, _json(response)) == (422, {"detail": [{"type": "missing", "loc": ["body", "items"],
                                                                         "msg": "Field required", "input": {}}]})


def test_without_cookie_the_route_keeps_the_guest_code():
    create = _Create()
    assert ORDERS.respond(_request([("Content-Type", "application/json")]), create) is None   # без Origin и CSRF
    assert create.cursors == []


def test_valid_session_links_the_order_in_the_same_transaction():
    session = SessionToken.new()
    create = _Create()
    response = ORDERS.respond(_request(_session_headers(session)), create)
    assert response.status_code == 201 and _json(response) == {**GUEST_BODY, "account_linked": True}
    assert _no_store(response) and response.headers.getlist("set-cookie") == []
    [cursor] = create.cursors
    assert cursor.calls == [(SQL_LINK_NEW_ORDER, (session.sha256, ORDER_ID))]                 # только хэш
    charset = ORDERS.respond(_request(_session_headers(session, **{"Content-Type": "application/json; charset=utf-8"})),
                             _Create())
    assert charset.status_code == 201


def _refusals():
    session, other = SessionToken.new(), SessionToken.new()
    return {
        "нет Origin": _session_headers(session, Origin=None),
        "чужой Origin": _session_headers(session, Origin=EVIL),
        "http-вариант Origin": _session_headers(session, Origin="http://shop.example.test"),
        "форма text/plain": _session_headers(session, **{"Content-Type": "text/plain"}),
        "форма urlencoded": _session_headers(session, **{"Content-Type": "application/x-www-form-urlencoded"}),
        "нет Content-Type": _session_headers(session, **{"Content-Type": None}),
        "нет CSRF": _session_headers(session, **{"X-CSRF-Token": None}),
        "неверный CSRF": _session_headers(session, csrf="A" * 43),
        "CSRF другой сессии": _session_headers(session, csrf=csrf_token(CSRF_KEY, other)),
        "повреждённая cookie": _session_headers(session, Cookie=f"{SESSION_COOKIE_NAME}=not-a-token"),
        "пустая cookie": _session_headers(session, Cookie=f"{SESSION_COOKIE_NAME}="),
        "два CSRF": _session_headers(session) + [("X-CSRF-Token", csrf_token(CSRF_KEY, session))],
    }


@pytest.mark.parametrize("name", list(_refusals()))
def test_origin_content_type_or_csrf_refusal_is_403_without_creating_the_order(name):
    create = _Create()
    response = ORDERS.respond(_request(_refusals()[name]), create)
    assert (response.status_code, _json(response)) == (403, CSRF_FAILED)
    assert create.cursors == [] and response.headers.getlist("set-cookie") == [] and _no_store(response)


@pytest.mark.parametrize("result", ["session_invalid", "disabled"])
def test_refused_link_is_401_with_cookie_removal(result):
    session = SessionToken.new()
    response = ORDERS.respond(_request(_session_headers(session)), _Create(answer={"result": result}))
    assert (response.status_code, _json(response)) == (401, SESSION_INVALID)
    assert response.headers.getlist("set-cookie") == [clear_session_cookie_header()] and _no_store(response)


@pytest.mark.parametrize("create", [
    _Create(answer={"result": "linked", "customer_id": 1}),                   # ответ функции вне контракта
    _Create(answer={"result": "ok"}),
    _Create(answer=None),
    _Create(error=psycopg.errors.RaiseException("SELECT secret FROM customer_sessions")),
    _Create(error=RuntimeError("SELECT secret FROM customer_sessions")),
    _Create(body={**GUEST_BODY, "account_linked": False}),                    # тело вне контракта
    _Create(body={"order": GUEST_BODY["order"]}),
    _Create(body=None),
], ids=["лишнее поле", "чужой результат", "null", "RAISE базы", "сбой", "лишний ключ тела", "нет секрета", "нет тела"])
def test_any_other_outcome_is_a_safe_json_500_without_guest_fallback(create, caplog):
    session = SessionToken.new()
    with caplog.at_level(logging.INFO, logger="digital_store.accounts"):
        response = ORDERS.respond(_request(_session_headers(session)), create)
    assert (response.status_code, _json(response)) == (500, INTERNAL_ERROR)
    assert response.headers.getlist("set-cookie") == [] and _no_store(response)
    assert len(create.cursors) == 1                                          # одна попытка, без гостевого повтора
    for text in (caplog.text, response.body.decode()):
        assert "SELECT" not in text and "secret" not in text and session.value not in text


def test_order_rule_errors_keep_the_guest_body_with_no_store():
    response = ORDERS.respond(_request(_session_headers(SessionToken.new())), _Create(error=_RuleError()),
                              business_errors=(_RuleError,))
    assert (response.status_code, _json(response)) == (404, {"detail": _RuleError().as_detail()})
    assert _no_store(response) and response.headers.getlist("set-cookie") == []


def test_constructor_rejects_wrong_arguments():
    with pytest.raises(TypeError):
        AccountOrders(object())
    for trusted in (None, OriginAllowlist([]), [SHOP]):
        with pytest.raises(TypeError):
            AccountOrders(SETTINGS, trusted)


def _validation(errors_input=SECRET_INPUT):
    return RequestValidationError([{"type": "extra_forbidden", "loc": ("body", "customer_id"),
                                    "msg": "Extra inputs are not permitted", "input": errors_input}])


def test_validation_error_with_session_cookie_hides_the_input():
    session = SessionToken.new()
    response = asyncio.run(ORDERS.validation_error(_request(_session_headers(session)), _validation()))
    assert (response.status_code, _json(response)) == (422, INVALID_REQUEST) and _no_store(response)
    assert SECRET_INPUT not in response.body.decode() and response.headers.getlist("set-cookie") == []
    # Сначала Origin, Content-Type и CSRF: межсайтовая форма с неверным телом — 403, а не 422.
    for name, headers in _refusals().items():
        refused = asyncio.run(ORDERS.validation_error(_request(headers), _validation()))
        assert (refused.status_code, _json(refused)) == (403, CSRF_FAILED), name


def test_validation_error_elsewhere_is_the_standard_fastapi_response():
    session = SessionToken.new()
    cases = [_request([("Content-Type", "application/json")]),                                # гость
             _request(_session_headers(session), path="/orders"),                             # API v1
             _request(_session_headers(session), path="/api/v2/orders/1/checkout"),
             _request(_session_headers(session), method="PUT")]
    for request in cases:
        expected = asyncio.run(request_validation_exception_handler(request, _validation()))
        response = asyncio.run(ORDERS.validation_error(request, _validation()))
        assert (response.status_code, response.body) == (expected.status_code, expected.body)
        assert SECRET_INPUT in response.body.decode() and not _no_store(response)              # как до PR6


def test_presents_session_only_for_the_order_path_with_the_cookie():
    def scope(path, cookie=True):
        headers = [(b"cookie", f"{SESSION_COOKIE_NAME}=x".encode())] if cookie else []
        return {"type": "http", "path": path, "headers": headers}
    assert presents_session(scope("/api/v2/orders")) and presents_session(scope("/api/v2/orders/"))
    for path in ("/api/v2/orders/1/checkout", "/orders", "/api/v2/products", "/api/v2/ordersx"):
        assert not presents_session(scope(path)), path
    assert not presents_session(scope("/api/v2/orders", cookie=False))
    assert not presents_session({"type": "http", "path": "/api/v2/orders",
                                 "headers": [(b"cookie", b"other=__Host-ds_session")]})


# ---------- AccountResponseGuard в сборке install_http_stack ----------

def _stack(settings=SETTINGS):
    app = FastAPI()

    @app.post("/api/v2/orders", status_code=201)
    async def boom(request: FastAPIRequest):
        raise RuntimeError("SELECT secret FROM customer_sessions")

    env = {"ACCOUNT_TRUSTED_ORIGINS": SHOP, "CORS_ALLOWED_ORIGINS": SHOP}
    install_http_stack(app, limits={"rate_limits": True, "trusted_proxies": ()}, cors_options=_base([SHOP]),
                       account_settings=settings, env=env, account_service=lambda s: FakeService())
    return app


def test_guard_turns_an_escaped_error_with_cookie_into_json_500_and_leaves_guests_alone():
    client = TestClient(_stack(), base_url="https://testserver", raise_server_exceptions=False)
    cookie = {"Cookie": f"{SESSION_COOKIE_NAME}={SessionToken.new().value}"}
    with_cookie = client.post("/api/v2/orders", headers=cookie, content=b"{}")
    client.cookies.clear()
    assert (with_cookie.status_code, with_cookie.json()) == (500, INTERNAL_ERROR)
    assert with_cookie.headers["cache-control"] == "no-store" and "SELECT" not in with_cookie.text
    guest = client.post("/api/v2/orders", content=b"{}")
    assert (guest.status_code, guest.text) == (500, "Internal Server Error") and "cache-control" not in guest.headers


def test_validation_handler_is_installed_only_with_accounts_enabled():
    enabled = _stack()
    handler = enabled.exception_handlers[RequestValidationError]
    assert isinstance(handler.__self__, AccountOrders) and handler.__func__ is AccountOrders.validation_error
    disabled = FastAPI()
    install_http_stack(disabled, limits={"rate_limits": True, "trusted_proxies": ()}, cors_options=_base([]),
                       account_settings=AccountSettings(), env={}, account_service=lambda s: pytest.fail("сервис"))
    assert disabled.exception_handlers[RequestValidationError] is request_validation_exception_handler


# ---------- настоящий api.py в отдельном процессе ----------

_PROBE = r'''
import json, logging, sys
project, mode = sys.argv[1], sys.argv[2]
sys.path[:0] = [project, project + "/tests"]
import dotenv, dotenv.main

def _no_dotenv(*args, **kwargs):
    raise AssertionError(".env не должен читаться")

dotenv.find_dotenv = dotenv.main.find_dotenv = _no_dotenv
dotenv.load_dotenv = dotenv.main.load_dotenv = lambda *args, **kwargs: False
import psycopg

def _no_database(*args, **kwargs):
    raise AssertionError("подключение к PostgreSQL запрещено")

psycopg.connect = _no_database
logging.basicConfig(stream=sys.stderr, level=logging.INFO)

import api
import database
if mode == "disabled":
    # Заголовок Cookie разбирает сам FastAPI для любого маршрута (solve_dependencies), как и до PR6. Ошибка — чтение
    # cookie кодом магазина и любой вызов функций сессии.
    import starlette.requests
    import accounts.order_http, accounts.order_link, accounts.routes, accounts.session
    _framework_cookies = starlette.requests.HTTPConnection.cookies.fget

    def _cookies(self):
        caller = sys._getframe(1).f_globals.get("__name__", "")
        if caller.split(".")[0] in ("accounts", "api", "http_stack", "payments", "commerce", "database"):
            raise AssertionError("cookie прочитана при выключенных аккаунтах")
        return _framework_cookies(self)

    def _session_read(*args, **kwargs):
        raise AssertionError("cookie сессии прочитана при выключенных аккаунтах")

    starlette.requests.HTTPConnection.cookies = property(_cookies)
    for module in (accounts.order_http, accounts.order_link, accounts.routes, accounts.session):
        for name in ("has_session_cookie", "session_from_cookies", "presents_session"):
            if hasattr(module, name):
                setattr(module, name, _session_read)
from fastapi.testclient import TestClient
from accounts.config import load_account_settings
from accounts.request_guard import csrf_token
from accounts.session import SessionToken
from test_account_order_link import _Connection

SHOP, EVIL = "https://shop.example.test", "https://evil.example.test"
SECRET_INPUT = "EXTRA-SECRET-VALUE"
LINKED = {"result": "linked"}
BODY = json.dumps({"items": [{"product_id": 5, "quantity": 2}]}).encode()
EXTRA = json.dumps({"items": [{"product_id": 5, "quantity": 2}], "customer_id": SECRET_INPUT}).encode()
MISSING_PRODUCT = json.dumps({"items": [{"product_id": 6, "quantity": 1}]}).encode()
connections, counter = [], [0]
api.create_order_in_db = lambda product_id: {"id": 9, "product_id": product_id, "status": "new"}   # v1 без базы


def send(method, path, headers=None, content=BODY, ip=None, **connection):
    def factory():
        conn = _Connection(**connection)
        connections.append(conn)
        return conn
    connections.clear()
    database.get_connection = factory
    counter[0] += 1
    client = TestClient(api.app, base_url="https://testserver", raise_server_exceptions=False,
                        client=(ip or f"10.0.{counter[0] // 200}.{counter[0] % 200 + 1}", 50000))
    response = client.request(method, path, headers=headers or {}, content=content)
    return {"status": response.status_code, "body": response.text, "headers": dict(response.headers),
            "set_cookie": response.headers.get_list("set-cookie"), "log": [conn.log for conn in connections],
            "params": [[step, repr(params)] for conn in connections for step, _, params in conn.calls]}


settings = load_account_settings()
session, other = SessionToken.new(), SessionToken.new()
csrf = csrf_token(settings.csrf_key, session) if settings.enabled else "c" * 43
cookie = "__Host-ds_session=" + session.value
GUEST = {"Content-Type": "application/json"}
B = {"Origin": SHOP, "Content-Type": "application/json", "Cookie": cookie, "X-CSRF-Token": csrf}


def without(name):
    return {key: value for key, value in B.items() if key != name}


NO_COOKIE = without("Cookie")                         # те же заголовки, что у B, но без cookie — гость
V1_422 = json.dumps({"product_id": SECRET_INPUT}).encode()
result = {
    "openapi": api.app.openapi()["paths"]["/api/v2/orders"],
    "guest": send("POST", "/api/v2/orders", GUEST),
    "guest_extra": send("POST", "/api/v2/orders", GUEST, EXTRA),
    "guest_missing_product": send("POST", "/api/v2/orders", GUEST, MISSING_PRODUCT),
    "guest_big": send("POST", "/api/v2/orders", GUEST, b"x" * (17 * 1024)),
    "guest_405": send("GET", "/api/v2/orders", {}, None),
    "v1": send("POST", "/orders", GUEST, b'{"product_id": 5}'),
    "v1_422": send("POST", "/orders", GUEST, V1_422),
    "no_cookie": send("POST", "/api/v2/orders", NO_COOKIE),
    "no_cookie_extra": send("POST", "/api/v2/orders", NO_COOKIE, EXTRA),
    "no_cookie_big": send("POST", "/api/v2/orders", NO_COOKIE, b"x" * (17 * 1024)),
    "no_cookie_v1": send("POST", "/orders", NO_COOKIE, b'{"product_id": 5}'),
    "no_cookie_v1_422": send("POST", "/orders", NO_COOKIE, V1_422),
    "cookie": send("POST", "/api/v2/orders", B, link_answer=LINKED),
    "cookie_extra": send("POST", "/api/v2/orders", B, EXTRA),
    "cookie_big": send("POST", "/api/v2/orders", B, b"x" * (17 * 1024)),
    "cookie_405": send("GET", "/api/v2/orders", {"Cookie": cookie}, None),
    "v1_cookie": send("POST", "/orders", B, b'{"product_id": 5}'),
    "v1_cookie_422": send("POST", "/orders", B, V1_422),
    "rate": [send("POST", "/api/v2/orders", B, ip="203.0.113.9", link_answer=LINKED) for _ in range(11)],
    "secrets": {"session": session.value, "csrf": csrf, "sha256": repr(session.sha256)},
}
if mode == "enabled":
    result.update({
        "charset": send("POST", "/api/v2/orders", {**B, "Content-Type": "application/json; charset=utf-8"},
                        link_answer=LINKED),
        "session_invalid": send("POST", "/api/v2/orders", B, link_answer={"result": "session_invalid"}),
        "db_disabled": send("POST", "/api/v2/orders", B, link_answer={"result": "disabled"}),
        "out_of_contract": send("POST", "/api/v2/orders", B, link_answer={"result": "linked", "customer_id": 1}),
        "link_raise": send("POST", "/api/v2/orders", B, fail_on="customer_order_link_new"),
        "access_raise": send("POST", "/api/v2/orders", B, fail_on="order_access_issue"),
        "missing_product": send("POST", "/api/v2/orders", B, MISSING_PRODUCT, link_answer=LINKED),
        "malformed": send("POST", "/api/v2/orders", B, ('{"items": [' + SECRET_INPUT).encode()),
        "empty_items": send("POST", "/api/v2/orders", B, b'{"items": []}'),
        "string_quantity": send("POST", "/api/v2/orders", B,
                                json.dumps({"items": [{"product_id": 5, "quantity": SECRET_INPUT}]}).encode()),
        "extra_bad_csrf": send("POST", "/api/v2/orders", {**B, "X-CSRF-Token": "A" * 43}, EXTRA),
        "refusals": {
            "no_origin": send("POST", "/api/v2/orders", without("Origin"), link_answer=LINKED),
            "evil_origin": send("POST", "/api/v2/orders", {**B, "Origin": EVIL}, link_answer=LINKED),
            "text_plain": send("POST", "/api/v2/orders", {**B, "Content-Type": "text/plain"}, link_answer=LINKED),
            "no_csrf": send("POST", "/api/v2/orders", without("X-CSRF-Token"), link_answer=LINKED),
            "wrong_csrf": send("POST", "/api/v2/orders", {**B, "X-CSRF-Token": "A" * 43}, link_answer=LINKED),
            "other_csrf": send("POST", "/api/v2/orders", {**B, "X-CSRF-Token": csrf_token(settings.csrf_key, other)},
                               link_answer=LINKED),
            "corrupted_cookie": send("POST", "/api/v2/orders", {**B, "Cookie": "__Host-ds_session=not-a-token"},
                                     link_answer=LINKED),
            "empty_cookie": send("POST", "/api/v2/orders", {**B, "Cookie": "__Host-ds_session="},
                                 link_answer=LINKED),
        },
    })
print(json.dumps(result, ensure_ascii=False))
'''

_OS_VARS = ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC")
ORDER_STEPS = ["SELECT products", "INSERT orders", "INSERT order_items", "order_access_issue"]


def _real_api(tmp_path, mode, extra_env):
    env = {name: os.environ[name] for name in _OS_VARS if name in os.environ}
    env.update({"PAYMENTS_ENABLED": "false"}, **extra_env)
    completed = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", _PROBE, str(PROJECT_DIR), mode],
                               capture_output=True, text=True, encoding="utf-8", env=env, cwd=tmp_path, timeout=180)
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout), completed.stderr


@pytest.fixture(scope="module")
def disabled_run(tmp_path_factory):
    return _real_api(tmp_path_factory.mktemp("disabled"), "disabled", {})


@pytest.fixture(scope="module")
def enabled_run(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("enabled")
    path, values = _secrets_file(tmp_path)
    result, stderr = _real_api(tmp_path, "enabled", {"DS_ACCOUNTS_ENABLED": "on", "DS_ACCOUNT_SECRETS_FILE": str(path),
                                                     "ACCOUNT_TRUSTED_ORIGINS": SHOP, "CORS_ALLOWED_ORIGINS": SHOP})
    return result, stderr, values


def _body(response):
    return json.loads(response["body"])


def _without_token(response):
    body = _body(response)
    assert len(body.pop("access_token")) == 43
    return body


def _same_response(left, right):
    """Один и тот же ответ: статус, заголовки и тело (секрет доступа у каждого заказа свой — сравнивается без него)."""
    assert (left["status"], left["headers"], left["set_cookie"]) == (right["status"], right["headers"],
                                                                     right["set_cookie"])
    if left["status"] == 201:
        assert _without_token(left) == _without_token(right)
    else:
        assert left["body"] == right["body"]


def test_real_api_disabled_accounts_read_no_cookie_and_keep_the_old_shop(disabled_run):
    """F2: с выключенными аккаунтами чтение cookie кодом магазина — ошибка; запрос с cookie — обычный гостевой."""
    result, _ = disabled_run
    guest = result["guest"]
    assert guest["status"] == 201 and set(_body(guest)) == {"order", "access_token"}
    assert guest["log"] == [ORDER_STEPS + ["COMMIT", "CLOSE"]] and "cache-control" not in guest["headers"]
    for cookie, plain in (("cookie", "no_cookie"), ("cookie_extra", "no_cookie_extra"), ("cookie_big", "no_cookie_big"),
                          ("cookie_405", "guest_405"), ("v1_cookie", "no_cookie_v1"),
                          ("v1_cookie_422", "no_cookie_v1_422")):
        _same_response(result[cookie], result[plain])
    assert result["cookie"]["log"] == [ORDER_STEPS + ["COMMIT", "CLOSE"]]               # привязки нет
    assert "account_linked" not in result["cookie"]["body"]
    assert SECRET_INPUT in result["cookie_extra"]["body"]                                 # прежний 422 FastAPI
    assert [r["status"] for r in result["rate"]] == [201] * 10 + [429]
    assert "cache-control" not in result["rate"][-1]["headers"]


def test_real_api_guest_contract_is_identical_with_accounts_enabled(disabled_run, enabled_run):
    disabled, _ = disabled_run
    enabled, _, _ = enabled_run
    for name in ("guest", "guest_extra", "guest_missing_product", "guest_big", "guest_405", "v1", "v1_422"):
        _same_response(enabled[name], disabled[name])
    # API v1 cookie не читает и при включённых аккаунтах; X-CSRF-Token и Origin без cookie — обычный гость.
    _same_response(enabled["v1_cookie"], enabled["no_cookie_v1"])
    _same_response(enabled["v1_cookie_422"], enabled["no_cookie_v1_422"])
    for name in ("guest", "no_cookie"):
        assert enabled[name]["log"] == [ORDER_STEPS + ["COMMIT", "CLOSE"]], name
        assert set(_body(enabled[name])) == {"order", "access_token"} and enabled[name]["set_cookie"] == []
        assert "cache-control" not in enabled[name]["headers"], name
    assert _without_token(enabled["no_cookie"]) == _without_token(enabled["guest"])
    assert SECRET_INPUT in enabled["guest_extra"]["body"] and SECRET_INPUT in enabled["no_cookie_extra"]["body"]
    assert enabled["openapi"] == disabled["openapi"] and "parameters" not in enabled["openapi"]["post"]
    schema = enabled["openapi"]["post"]["responses"]["201"]["content"]["application/json"]["schema"]
    assert schema == {"$ref": "#/components/schemas/OrderCreatedV2"}


def test_real_api_valid_session_links_the_order(enabled_run):
    result, _, _ = enabled_run
    guest, secrets = result["guest"], result["secrets"]
    for name in ("cookie", "charset"):
        linked = result[name]
        assert linked["status"] == 201 and linked["headers"]["cache-control"] == "no-store", name
        body = _body(linked)
        assert body.pop("account_linked") is True and set(body) == {"order", "access_token"}
        assert _without_token(linked) == {**_without_token(guest), "account_linked": True}
        assert linked["set_cookie"] == [] and linked["headers"]["access-control-allow-origin"] == SHOP
        assert linked["log"] == [ORDER_STEPS + ["customer_order_link_new", "COMMIT", "CLOSE"]]
    link_params = [params for step, params in result["cookie"]["params"] if step == "customer_order_link_new"]
    assert link_params == [f"({secrets['sha256']}, 41)"]                                 # SHA-256 и id — не токен
    assert not any(secrets["session"] in params or secrets["csrf"] in params for _, params in result["cookie"]["params"])


def test_real_api_refusals_are_403_before_the_order_exists(enabled_run):
    result, _, _ = enabled_run
    for name, response in {**result["refusals"], "extra_bad_csrf": result["extra_bad_csrf"]}.items():
        assert (response["status"], _body(response)) == (403, CSRF_FAILED), name
        assert response["log"] == [] and response["set_cookie"] == [], name               # заказа нет, cookie та же
        assert response["headers"]["cache-control"] == "no-store", name


def test_real_api_invalid_session_is_401_rollback_and_cookie_removal(enabled_run):
    result, _, _ = enabled_run
    for name in ("session_invalid", "db_disabled"):
        response = result[name]
        assert (response["status"], _body(response)) == (401, SESSION_INVALID), name
        assert response["set_cookie"] == [clear_session_cookie_header()]
        assert response["headers"]["cache-control"] == "no-store"
        assert response["log"] == [ORDER_STEPS + ["customer_order_link_new", "ROLLBACK", "CLOSE"]]   # без fallback


def test_real_api_unexpected_outcomes_are_safe_json_500(enabled_run):
    result, _, _ = enabled_run
    for name, steps in (("out_of_contract", ORDER_STEPS + ["customer_order_link_new"]),
                        ("link_raise", ORDER_STEPS + ["customer_order_link_new"]), ("access_raise", ORDER_STEPS)):
        response = result[name]
        assert (response["status"], _body(response)) == (500, INTERNAL_ERROR), name
        assert response["headers"]["cache-control"] == "no-store" and response["set_cookie"] == []
        assert response["log"] == [steps + ["ROLLBACK", "CLOSE"]], name                   # одна транзакция, откат


def test_real_api_order_rule_errors_with_cookie_match_the_guest(enabled_run):
    result, _, _ = enabled_run
    linked, guest = result["missing_product"], result["guest_missing_product"]
    assert linked["status"] == guest["status"] == 404 and _body(linked) == _body(guest)
    assert linked["headers"]["cache-control"] == "no-store" and "cache-control" not in guest["headers"]
    assert linked["log"] == [["SELECT products", "ROLLBACK", "CLOSE"]] and linked["set_cookie"] == []


def test_real_api_invalid_body_with_cookie_is_a_safe_422(enabled_run):
    result, _, _ = enabled_run
    secrets = result["secrets"]
    for name in ("cookie_extra", "malformed", "empty_items", "string_quantity"):
        response = result[name]
        assert (response["status"], _body(response)) == (422, INVALID_REQUEST), name
        assert response["headers"]["cache-control"] == "no-store" and response["set_cookie"] == []
        assert response["log"] == [], name
        for value in (SECRET_INPUT, secrets["session"], secrets["csrf"], "customer_id", "input"):
            assert value not in response["body"], (name, value)


def test_real_api_middleware_responses_with_cookie_are_no_store(enabled_run):
    result, _, _ = enabled_run
    big, too_many, wrong_method = result["cookie_big"], result["rate"][-1], result["cookie_405"]
    assert [r["status"] for r in result["rate"]] == [201] * 10 + [429]                   # общий лимит orders_create
    assert all(r["headers"]["cache-control"] == "no-store" for r in result["rate"])
    assert big["status"] == 413 and too_many["status"] == 429 and wrong_method["status"] == 405
    for response in (big, too_many, wrong_method):
        assert response["headers"]["cache-control"] == "no-store" and response["log"] == []
    assert too_many["headers"]["retry-after"].isdigit()
    assert big["headers"]["access-control-allow-origin"] == SHOP                         # CORS — как прежде
    for name in ("guest_big", "no_cookie_big", "guest_405"):                             # без cookie — по-старому
        assert "cache-control" not in result[name]["headers"], name


def test_real_api_output_has_no_secrets(enabled_run):
    result, stderr, values = enabled_run
    secrets = result["secrets"]
    echoed = ("secrets", "guest_extra", "no_cookie_extra", "v1_422", "no_cookie_v1_422", "v1_cookie_422")  # гость, v1
    output = stderr + json.dumps({key: value for key, value in result.items() if key not in echoed},
                                 ensure_ascii=False)
    for value in list(values.values()) + [value.split(":", 1)[-1] for value in values.values()]:
        assert value not in output
    for value in (secrets["session"], secrets["csrf"], SECRET_INPUT):
        assert value not in output
    assert "csrf_invalid" in stderr and "session_invalid" in stderr                      # причины — только коды
