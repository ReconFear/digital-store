"""PR6 этап 2d-3: сборка HTTP-слоёв приложения (http_stack.py) и подключение аккаунтов в api.py — офлайн.

Две части:
  * install_http_stack — та же функция, что вызывает api.py, на тестовом FastAPI с заглушками маршрутов магазина
    (поддельный AccountService): порядок слоёв, no-store на ответах middleware и ошибках, CORS, лимиты;
  * настоящий api.py в отдельном процессе Python: python-dotenv обезврежен ДО импорта (load_dotenv ничего не
    делает, find_dotenv падает — .env не читается), psycopg.connect падает (к базе не подключиться), платежи
    выключены. Проверяются оба режима: по умолчанию (аккаунты выключены) и включённый с тестовым файлом секретов.
"""

import base64
import json
import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

import accounts.config
import accounts.routes
import accounts.session
from accounts.config import AccountSettings, load_account_settings
from accounts.request_guard import csrf_token
from accounts.session import SESSION_COOKIE_NAME, SessionToken, clear_session_cookie_header
from accounts.web import ACCOUNT_BODY_LIMITS, AccountWebConfigError
from http_security import RequestLimitsMiddleware, SensitiveResponseHeadersMiddleware
from http_stack import AccountResponseGuard, install_http_stack
from test_account_http import ALL_ROUTES, CREDENTIALS, SETTINGS, FakeService
from test_account_web_config import _base, _Untouchable

PROJECT_DIR = Path(__file__).resolve().parent.parent
SHOP = "https://shop.example.test"
EVIL = "https://evil.example.test"
DEV = "http://localhost:5500"
ENABLED_ENV = {"ACCOUNT_TRUSTED_ORIGINS": SHOP, "CORS_ALLOWED_ORIGINS": SHOP}
LIMITS = {"rate_limits": True, "trusted_proxies": ()}
JSON_FROM_SHOP = {"Origin": SHOP, "Content-Type": "application/json"}


# ---------- стенд: install_http_stack на заглушках маршрутов магазина ----------

def _shop(settings=SETTINGS, env=ENABLED_ENV, cors_origins=(SHOP,), limits=LIMITS, service=None, log=None):
    app = FastAPI()

    @app.post("/api/v2/orders", status_code=201)
    async def create_order(request: Request):
        return {"order": {"id": 1}}

    @app.post("/api/v2/orders/{order_id}/delivery")
    async def delivery(order_id: int):
        return {"keys": []}

    @app.get("/api/v2/boom")
    async def guest_boom():
        raise RuntimeError("ошибка гостевого маршрута")

    @app.get("/api/account/boom")
    async def account_boom():
        raise RuntimeError("SELECT secret FROM somewhere")                # не должно попасть в ответ

    created = []

    def factory(account_settings):
        created.append(account_settings)
        return service if service is not None else FakeService()

    web = install_http_stack(app, limits=limits, cors_options=_base(cors_origins), account_settings=settings,
                             env=env, account_service=factory, log=log)
    return app, web, created


def _client(app, ip="198.51.100.7"):
    return TestClient(app, base_url="https://testserver", client=(ip, 50000), raise_server_exceptions=False)


def _send(client, method, path, *, headers=None, body=None, raw=None):
    content = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    response = client.request(method, path, headers=headers or {}, content=content)
    client.cookies.clear()
    return response


def _cookie(session):
    return {"Cookie": f"{SESSION_COOKIE_NAME}={session.value}"}


def _preflight(client, path, origin, request_headers="content-type, x-csrf-token"):
    return _send(client, "OPTIONS", path, headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                                   "Access-Control-Request-Headers": request_headers})


def _layers(app):
    return [(m.cls, m.args, m.kwargs) for m in app.user_middleware]


def _no_store(response):
    return response.headers.get("cache-control") == "no-store"


def _account_paths(app):
    """Пути раздела аккаунтов (FastAPI хранит подключённый APIRouter целиком, поэтому — через OpenAPI)."""
    return {path for path in app.openapi()["paths"] if path.startswith("/api/account") and path != "/api/account/boom"}


ACCOUNT_PATHS = {"/api/account/register", "/api/account/login", "/api/account/logout", "/api/account/logout-all",
                 "/api/account/password", "/api/account/me", "/api/account/orders", "/api/account/orders/{order_id}"}


# ---------- выключенные аккаунты: ровно прежний магазин ----------

def test_disabled_stack_is_exactly_the_pre_pr6_stack():
    """До PR6 api.py добавлял RequestLimitsMiddleware(**limits), SensitiveResponseHeadersMiddleware() и
    CORSMiddleware(**cors_middleware_options()) — в этом порядке и с этими параметрами."""
    app, web, created = _shop(settings=AccountSettings(), env=_Untouchable(), cors_origins=(DEV,))
    assert web is None and created == []                               # окружение и сервис не тронуты
    assert _layers(app) == [(CORSMiddleware, (), _base([DEV])), (SensitiveResponseHeadersMiddleware, (), {}),
                            (RequestLimitsMiddleware, (), LIMITS)]
    assert _account_paths(app) == set()


def test_disabled_accounts_are_missing_routes_and_read_no_cookie_or_secrets(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise AssertionError("при выключенных аккаунтах это не вызывается")
    monkeypatch.setattr(accounts.config, "_read_secrets_file", boom)
    monkeypatch.setattr(accounts.routes, "session_from_cookies", boom)
    monkeypatch.setattr(accounts.session, "session_from_cookies", boom)
    settings = load_account_settings({"DS_ACCOUNT_SECRETS_FILE": str(tmp_path / "нет-такого-файла")})
    assert settings.enabled is False                                    # по умолчанию выключены
    app, _, _ = _shop(settings=settings, env=_Untouchable(), cors_origins=(DEV,))
    client = _client(app)
    missing = _send(client, "GET", "/api/account/no-such-route")
    session = SessionToken.new()
    for method, path in ALL_ROUTES:
        response = _send(client, method, path, body=CREDENTIALS, headers={**JSON_FROM_SHOP, **_cookie(session),
                                                                           "X-CSRF-Token": "x" * 43})
        assert (response.status_code, response.content) == (404, missing.content), path
        assert not response.headers.get_list("set-cookie") and "cache-control" not in response.headers
    with_cookie = _send(client, "POST", "/api/v2/orders", body={}, headers={"Content-Type": "application/json",
                                                                            **_cookie(session)})
    without = _send(client, "POST", "/api/v2/orders", body={}, headers={"Content-Type": "application/json"})
    assert (with_cookie.status_code, with_cookie.content) == (without.status_code, without.content) == (
        201, b'{"order":{"id":1}}')


def test_disabled_cors_keeps_old_options_without_credentials_or_csrf_header():
    app, _, _ = _shop(settings=AccountSettings(), env=_Untouchable(), cors_origins=(DEV,))
    client = _client(app)
    allowed = _preflight(client, "/api/v2/orders", DEV, "content-type")
    assert allowed.status_code == 200 and allowed.headers["access-control-allow-origin"] == DEV
    assert "access-control-allow-credentials" not in allowed.headers
    assert _preflight(client, "/api/v2/orders", DEV).status_code == 400    # X-CSRF-Token не разрешён
    assert _preflight(client, "/api/v2/orders", SHOP, "content-type").status_code == 400


# ---------- включённые аккаунты ----------

def test_enabled_stack_order_and_parameters():
    app, web, created = _shop()
    assert created == [SETTINGS]
    assert [layer[0] for layer in _layers(app)] == [AccountResponseGuard, CORSMiddleware,
                                                    SensitiveResponseHeadersMiddleware, RequestLimitsMiddleware]
    cors, limits = _layers(app)[1][2], _layers(app)[3][2]
    assert cors == web.cors_options and cors["allow_credentials"] is True and cors["allow_origins"] == [SHOP]
    assert "X-CSRF-Token" in cors["allow_headers"]
    # Общие лимиты и 4 КиБ — в middleware; правила аккаунтов — только в маршрутах (не дублируются).
    assert limits == {**LIMITS, "path_body_limits": ACCOUNT_BODY_LIMITS} and "rules" not in limits
    assert _account_paths(app) == ACCOUNT_PATHS


@pytest.mark.parametrize("env", [{}, {"ACCOUNT_TRUSTED_ORIGINS": SHOP}, {"ACCOUNT_TRUSTED_ORIGINS": "http://shop.test",
                                                                          "CORS_ALLOWED_ORIGINS": ""},
                                 {"ACCOUNT_TRUSTED_ORIGINS": SHOP, "CORS_ALLOWED_ORIGINS": EVIL}])
def test_invalid_account_configuration_stops_before_changing_the_app(env):
    app = FastAPI()
    cors_origins = [env["CORS_ALLOWED_ORIGINS"]] if env.get("CORS_ALLOWED_ORIGINS") else []
    with pytest.raises(AccountWebConfigError):
        install_http_stack(app, limits=LIMITS, cors_options=_base(cors_origins), account_settings=SETTINGS,
                           env=env, account_service=lambda s: pytest.fail("сервис создан до проверки настроек"))
    assert app.user_middleware == [] and _account_paths(app) == set()


def _secrets_file(tmp_path):
    """Тестовый файл секретов аккаунтов (случайные значения, не настоящие секреты)."""
    def b64():
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    values = {"ACCOUNT_PEPPER_CURRENT": f"1:{b64()}", "ACCOUNT_CSRF_KEY": b64()}
    path = tmp_path / "account-secrets.env"
    path.write_text("".join(f'{name}="{value}"\n' for name, value in values.items()), encoding="utf-8")
    return path, values


def test_enabled_settings_come_from_the_separate_secrets_file(tmp_path):
    path, _ = _secrets_file(tmp_path)
    settings = load_account_settings({"DS_ACCOUNTS_ENABLED": "on", "DS_ACCOUNT_SECRETS_FILE": str(path)})
    app, _, created = _shop(settings=settings)
    assert created == [settings]
    session = SessionToken.new()
    response = _send(_client(app), "GET", "/api/account/me", headers=_cookie(session))
    assert response.status_code == 200 and response.json()["csrf_token"] == csrf_token(settings.csrf_key, session)


def test_every_account_response_is_no_store_including_middleware_responses_and_errors():
    app, _, _ = _shop(service=FakeService(profile=None))
    client = _client(app)
    session = SessionToken.new()
    responses = {
        "201 маршрут": _send(client, "POST", "/api/account/register", body=CREDENTIALS, headers=JSON_FROM_SHOP),
        "429 маршрут": _send(client, "POST", "/api/account/register", body=CREDENTIALS, headers=JSON_FROM_SHOP),
        "403 маршрут": _send(client, "POST", "/api/account/login", body=CREDENTIALS,
                             headers={"Origin": EVIL, "Content-Type": "application/json"}),
        "422 маршрут": _send(client, "POST", "/api/account/login", raw=b"{", headers=JSON_FROM_SHOP),
        "401 маршрут": _send(client, "GET", "/api/account/me", headers=_cookie(session)),
        "404 нет маршрута": _send(client, "GET", "/api/account/no-such-route"),
        "404 раздел": _send(client, "GET", "/api/account"),
        "405": _send(client, "GET", "/api/account/login"),
        "413 middleware": _send(client, "POST", "/api/account/login", raw=b"x" * 5000, headers=JSON_FROM_SHOP),
        "preflight 200": _preflight(client, "/api/account/me", SHOP),
        "preflight 400": _preflight(client, "/api/account/me", EVIL),
        "500 исключение": _send(client, "GET", "/api/account/boom"),
    }
    expected = {"201 маршрут": 201, "429 маршрут": 429, "403 маршрут": 403, "422 маршрут": 422, "401 маршрут": 401,
                "404 нет маршрута": 404, "404 раздел": 404, "405": 405, "413 middleware": 413,
                "preflight 200": 200, "preflight 400": 400, "500 исключение": 500}
    assert {name: r.status_code for name, r in responses.items()} == expected
    assert [name for name, r in responses.items() if not _no_store(r)] == []
    error = responses["500 исключение"]
    assert error.json() == {"detail": {"code": "internal_error", "message": "Внутренняя ошибка."}}
    assert "SELECT" not in error.text and "secret" not in error.text
    # Прежние пути не меняются: ошибка гостевого маршрута — обычный ответ Starlette, выдача ключей — no-store PR5.
    guest = _send(client, "GET", "/api/v2/boom")
    assert guest.status_code == 500 and guest.text == "Internal Server Error" and not _no_store(guest)
    assert _no_store(_send(client, "POST", "/api/v2/orders/1/delivery"))
    assert not _no_store(_send(client, "POST", "/api/v2/orders", body={}, headers={"Content-Type": "application/json"}))


def test_cors_credentials_only_for_the_trusted_https_origin():
    app, _, _ = _shop()
    client = _client(app)
    session = SessionToken.new()
    trusted = _send(client, "GET", "/api/account/me", headers={"Origin": SHOP, **_cookie(session)})
    assert trusted.headers["access-control-allow-origin"] == SHOP
    assert trusted.headers["access-control-allow-credentials"] == "true"
    for origin in (EVIL, DEV, "http://shop.example.test", "null", SHOP + ".evil.test"):
        response = _send(client, "GET", "/api/account/me", headers={"Origin": origin, **_cookie(session)})
        assert "access-control-allow-origin" not in response.headers, origin
        preflight = _preflight(client, "/api/account/me", origin)
        assert preflight.status_code == 400 and "access-control-allow-origin" not in preflight.headers, origin
    preflight = _preflight(client, "/api/account/password", SHOP)
    assert preflight.status_code == 200 and "x-csrf-token" in preflight.headers["access-control-allow-headers"].lower()
    too_large = _send(client, "POST", "/api/account/login", raw=b"x" * 5000, headers=JSON_FROM_SHOP)
    assert too_large.status_code == 413 and too_large.headers["access-control-allow-origin"] == SHOP


def test_account_limits_live_in_routes_and_are_not_duplicated():
    app, _, _ = _shop()
    client = _client(app)
    evil = {"Origin": EVIL, "Content-Type": "application/json"}
    for _ in range(15):                                                  # отказы по Origin квоту не тратят
        assert _send(client, "POST", "/api/account/login", body=CREDENTIALS, headers=evil).status_code == 403
    statuses = [_send(client, "POST", "/api/account/login", body=CREDENTIALS, headers=JSON_FROM_SHOP).status_code
                for _ in range(11)]
    assert statuses == [200] * 10 + [429]                                # 10/мин на IP — один раз, в маршруте
    off, _, _ = _shop(limits={"rate_limits": False, "trusted_proxies": ()})
    client = _client(off)
    assert {_send(client, "POST", "/api/account/register", body=CREDENTIALS, headers=JSON_FROM_SHOP).status_code
            for _ in range(3)} == {201}


def test_logout_clears_cookie_through_the_whole_stack():
    app, _, _ = _shop()
    response = _send(_client(app), "POST", "/api/account/logout", body={},
                     headers={**JSON_FROM_SHOP, **_cookie(SessionToken.new())})
    assert response.status_code == 200 and response.headers.get_list("set-cookie") == [clear_session_cookie_header()]
    assert response.headers["access-control-allow-origin"] == SHOP and _no_store(response)


def test_proxy_warning_is_logged_without_secrets(tmp_path, caplog):
    path, values = _secrets_file(tmp_path)
    settings = load_account_settings({"DS_ACCOUNTS_ENABLED": "on", "DS_ACCOUNT_SECRETS_FILE": str(path)})
    with caplog.at_level(logging.WARNING, logger="digital_store.accounts"):
        _shop(settings=settings)
    text = caplog.text
    assert "TRUSTED_PROXY_IPS" in text and "в каждом процессе отдельно" in text
    for value in list(values.values()) + [value.split(":", 1)[-1] for value in values.values()] + [str(path)]:
        assert value not in text


def test_api_py_builds_its_stack_only_through_install_http_stack():
    """Одна production-сборка: api.py не добавляет слои сам, а вызывает проверяемую здесь функцию."""
    source = (PROJECT_DIR / "api.py").read_text(encoding="utf-8")
    assert "add_middleware" not in source and source.count("install_http_stack(") == 1
    assert "_account_settings = load_account_settings()" in source and source.count("load_account_settings(") == 1
    assert "account_settings=_account_settings" in source and "account_service=AccountService" in source


# ---------- настоящий api.py в отдельном процессе (без .env и базы) ----------

_PROBE = r'''
import json, logging, sys
sys.path.insert(0, sys.argv[1])
import dotenv, dotenv.main

def _no_dotenv(*args, **kwargs):
    raise AssertionError(".env не должен читаться")

dotenv.find_dotenv = dotenv.main.find_dotenv = _no_dotenv
dotenv.load_dotenv = dotenv.main.load_dotenv = lambda *args, **kwargs: False
import psycopg

def _no_database(*args, **kwargs):
    raise AssertionError("подключение к PostgreSQL запрещено")

psycopg.connect = _no_database
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

import api
from fastapi.testclient import TestClient

SHOP, EVIL, DEV = "https://shop.example.test", "https://evil.example.test", "http://localhost:5500"
client = TestClient(api.app, base_url="https://testserver", raise_server_exceptions=False)
COOKIE = {"Cookie": "__Host-ds_session=" + "A" * 43}
JSON_SHOP = {"Origin": SHOP, "Content-Type": "application/json"}


def send(method, path, headers=None, content=None):
    response = client.request(method, path, headers=headers or {}, content=content)
    client.cookies.clear()
    return {"status": response.status_code, "body": response.text, "headers": dict(response.headers),
            "set_cookie": response.headers.get_list("set-cookie")}


def preflight(path, origin, request_headers):
    return send("OPTIONS", path, {"Origin": origin, "Access-Control-Request-Method": "POST",
                                  "Access-Control-Request-Headers": request_headers})


layers = api.app.user_middleware
result = {
    "accounts_web": api.accounts_web is not None,
    "account_routes": sorted(p for p in api.app.openapi()["paths"] if p.startswith("/api/account")),
    "middleware": [m.cls.__name__ for m in layers],
    "cors": [m.kwargs for m in layers if m.cls.__name__ == "CORSMiddleware"][0],
    "limits": [sorted(m.kwargs) for m in layers if m.cls.__name__ == "RequestLimitsMiddleware"][0],
    "unknown": send("GET", "/api/account/no-such-route"),
    "login_evil": send("POST", "/api/account/login", {"Origin": EVIL, "Content-Type": "application/json", **COOKIE},
                       b'{"email": "buyer@example.test", "password": "secret-password-1"}'),
    "me_anonymous": send("GET", "/api/account/me"),
    "big_login": send("POST", "/api/account/login", JSON_SHOP, b"x" * 5000),
    "preflight_shop": preflight("/api/account/me", SHOP, "content-type, x-csrf-token"),
    "preflight_evil": preflight("/api/account/me", EVIL, "content-type"),
    "preflight_dev": preflight("/api/v2/orders", DEV, "content-type"),
    "preflight_dev_csrf": preflight("/api/v2/orders", DEV, "content-type, x-csrf-token"),
    "guest_order_cookie": send("POST", "/api/v2/orders", {"Content-Type": "application/json", **COOKIE}, b"{}"),
    "guest_order": send("POST", "/api/v2/orders", {"Content-Type": "application/json"}, b"{}"),
}
print(json.dumps(result))
'''

_OS_VARS = ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC")


def _real_api(tmp_path, extra_env):
    env = {name: os.environ[name] for name in _OS_VARS if name in os.environ}
    env.update({"PAYMENTS_ENABLED": "false"}, **extra_env)
    completed = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", _PROBE, str(PROJECT_DIR)],
                               capture_output=True, text=True, encoding="utf-8", env=env, cwd=tmp_path, timeout=120)
    assert completed.returncode == 0, completed.stderr[-4000:]
    return json.loads(completed.stdout), completed.stderr


def test_real_api_with_default_environment_is_the_old_shop(tmp_path):
    result, _ = _real_api(tmp_path, {})
    assert result["accounts_web"] is False and result["account_routes"] == []
    assert result["middleware"] == ["CORSMiddleware", "SensitiveResponseHeadersMiddleware", "RequestLimitsMiddleware"]
    assert result["cors"]["allow_credentials"] is False and DEV in result["cors"]["allow_origins"]
    assert result["cors"]["allow_headers"] == ["Content-Type"]
    assert result["limits"] == ["rate_limits", "trusted_proxies"]
    for name in ("login_evil", "me_anonymous", "big_login"):
        response = result[name]
        assert (response["status"], response["body"]) == (404, result["unknown"]["body"]) == (
            404, '{"detail":"Not Found"}'), name
        assert response["set_cookie"] == [] and "cache-control" not in response["headers"]
    assert result["preflight_shop"]["status"] == 400 and result["preflight_dev_csrf"]["status"] == 400
    dev = result["preflight_dev"]
    assert dev["status"] == 200 and dev["headers"]["access-control-allow-origin"] == DEV
    assert "access-control-allow-credentials" not in dev["headers"]
    assert result["guest_order_cookie"]["status"] == result["guest_order"]["status"] == 422
    assert result["guest_order_cookie"]["body"] == result["guest_order"]["body"]


def test_real_api_with_accounts_enabled(tmp_path):
    path, values = _secrets_file(tmp_path)
    result, stderr = _real_api(tmp_path, {"DS_ACCOUNTS_ENABLED": "on", "DS_ACCOUNT_SECRETS_FILE": str(path),
                                          "ACCOUNT_TRUSTED_ORIGINS": SHOP, "CORS_ALLOWED_ORIGINS": SHOP})
    assert result["accounts_web"] is True
    assert result["account_routes"] == sorted(ACCOUNT_PATHS)
    assert result["middleware"] == ["AccountResponseGuard", "CORSMiddleware", "SensitiveResponseHeadersMiddleware",
                                    "RequestLimitsMiddleware"]
    cors = result["cors"]
    assert cors["allow_credentials"] is True and cors["allow_origins"] == [SHOP]
    assert "X-CSRF-Token" in cors["allow_headers"]
    assert result["limits"] == ["path_body_limits", "rate_limits", "trusted_proxies"]
    expected = {"unknown": 404, "login_evil": 403, "me_anonymous": 401, "big_login": 413, "preflight_shop": 200,
                "preflight_evil": 400}
    assert {name: result[name]["status"] for name in expected} == expected
    assert all(result[name]["headers"].get("cache-control") == "no-store" for name in expected)
    assert result["me_anonymous"]["set_cookie"] == [clear_session_cookie_header()]
    assert result["login_evil"]["set_cookie"] == []
    shop = result["preflight_shop"]["headers"]
    assert shop["access-control-allow-origin"] == SHOP and shop["access-control-allow-credentials"] == "true"
    assert "access-control-allow-origin" not in result["preflight_evil"]["headers"]
    assert result["big_login"]["headers"]["access-control-allow-origin"] == SHOP
    assert result["preflight_dev"]["status"] == 400                      # http-адрес разработки не в списке
    # Этап 2e-2: заказ с cookie — случай B (подробно — test_account_order_http.py): без Origin и CSRF — 403,
    # cookie не трогается; гостевой заказ без cookie — прежний стандартный 422 FastAPI.
    with_cookie = result["guest_order_cookie"]
    assert (with_cookie["status"], json.loads(with_cookie["body"])["detail"]["code"]) == (403, "csrf_failed")
    assert with_cookie["set_cookie"] == [] and with_cookie["headers"]["cache-control"] == "no-store"
    guest = result["guest_order"]
    assert guest["status"] == 422 and json.loads(guest["body"])["detail"][0]["type"] == "missing"
    assert "cache-control" not in guest["headers"]
    assert "TRUSTED_PROXY_IPS" in stderr
    output = stderr + json.dumps(result, ensure_ascii=False)
    for value in list(values.values()) + [value.split(":", 1)[-1] for value in values.values()]:
        assert value not in output
    assert "secret-password-1" not in output
