"""PR6 этап 2d-2: HTTP-маршруты аккаунтов (accounts/routes.py) — офлайн, настоящие FastAPI/Starlette.

Сервис — поддельный (записывает вызовы, отвечает заданным), базы и api.py нет. Приложение собирается как
будет в api.py (2d-3): RequestLimitsMiddleware (общие лимиты, тело /api/account/* ≤ 4 КиБ) и no-store для
/api/account/*. Проверяется: контракт ответов, cookie только при успехе с сессией (T-H2, C1, C2), атрибуты
cookie (T-H3), Origin/CSRF (T-H5, T-H7), выход X6, T-H6, отсутствие утечек и порядок защит: отказ по Origin,
CSRF или формату не расходует квоту и не доходит до сервиса.
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import accounts.routes
from accounts.config import AccountSettings, Pepper
from accounts.proof import password_proof
from accounts.request_guard import OriginAllowlist, csrf_token
from accounts.routes import (
    LOGIN_PATH, LOGOUT_ALL_PATH, LOGOUT_PATH, ME_PATH, ORDERS_PATH, PASSWORD_PATH, REGISTER_PATH,
    build_account_router,
)
from accounts.service import AccountServiceError, SessionOutcome
from accounts.session import SESSION_COOKIE_NAME, SessionToken, clear_session_cookie_header, session_cookie_header
from accounts.web import ACCOUNT_BODY_LIMITS, ACCOUNT_PATH
from http_security import RequestLimitsMiddleware, SensitiveResponseHeadersMiddleware, SlidingWindowLimiter
from test_http_security import _Clock

SHOP = "https://shop.example.test"
TRUSTED = OriginAllowlist([SHOP])
PEPPER = Pepper(1, bytes(range(32)))
CSRF_KEY = bytes(range(64, 96))
SETTINGS = AccountSettings(enabled=True, pepper_current=PEPPER, csrf_key=CSRF_KEY)
EMAIL = "buyer@example.test"
PASSWORD = "Секретный пароль 2026"
NEW_PASSWORD = "Новый секретный пароль 2027"
CREDENTIALS = {"email": EMAIL, "password": PASSWORD}
PROFILE = {"email": EMAIL, "created_at": "2026-10-17T12:00:00+03:00"}
ORDER = {"id": 7, "status": "pending", "created_at": "2026-10-17T12:00:00+03:00", "total_minor": 10000,
         "currency": "RUB"}
ITEM = {"product_id": 3, "name": "Ключ", "product_type": "software_key", "quantity": 1, "unit_price_minor": 10000,
        "line_total_minor": 10000}
ALL_ROUTES = [("POST", REGISTER_PATH), ("POST", LOGIN_PATH), ("POST", LOGOUT_PATH), ("POST", LOGOUT_ALL_PATH),
              ("POST", PASSWORD_PATH), ("GET", ME_PATH), ("GET", ORDERS_PATH), ("GET", ORDERS_PATH + "/7")]


# ---------- поддельный сервис и стенд ----------

_DEFAULTS = {
    "register": lambda *args: SessionOutcome("created", session=SessionToken.new()),
    "login": lambda *args: SessionOutcome("ok", session=SessionToken.new()),
    "change_password": lambda *args: SessionOutcome("ok", session=SessionToken.new()),
    "resolve": True, "touch": True, "logout": None, "logout_all": "ok", "profile": PROFILE, "orders": [],
    "order": None,
}


class FakeService:
    """Поддельный AccountService: тот же интерфейс, ответы заданы заранее, вызовы записываются."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls = []

    def _answer(self, name, *args):
        self.calls.append((name,) + args)
        answer = self.answers.get(name, _DEFAULTS[name])
        if isinstance(answer, BaseException):
            raise answer
        return answer(*args) if callable(answer) else answer

    def names(self):
        return [call[0] for call in self.calls]

    def register(self, email_norm, password):
        return self._answer("register", email_norm, password)

    def login(self, email_norm, password, previous_session=None):
        return self._answer("login", email_norm, password, previous_session)

    def change_password(self, session, current_password, new_password):
        return self._answer("change_password", session, current_password, new_password)

    def resolve(self, session):
        return self._answer("resolve", session)

    def touch(self, session):
        return self._answer("touch", session)

    def logout(self, session):
        return self._answer("logout", session)

    def logout_all(self, session):
        return self._answer("logout_all", session)

    def profile(self, session):
        return self._answer("profile", session)

    def orders(self, session):
        return self._answer("orders", session)

    def order(self, session, order_id):
        return self._answer("order", session, order_id)


def _app(service=None, *, settings=SETTINGS, rate_limits=True, stack=True, clock=None):
    app = FastAPI()
    app.include_router(build_account_router(
        settings, service if service is not None else FakeService(), TRUSTED if settings.enabled else None,
        rate_limits=rate_limits, limiter=SlidingWindowLimiter(clock=clock or _Clock())))
    if stack:
        # Как в api.py (2d-3): no-store для /api/account/*, общие лимиты и 4 КиБ тела.
        app.add_middleware(RequestLimitsMiddleware, path_body_limits=ACCOUNT_BODY_LIMITS)
        app.add_middleware(SensitiveResponseHeadersMiddleware, path=ACCOUNT_PATH)
    return app


def _client(app, ip="198.51.100.7"):
    return TestClient(app, base_url="https://testserver", client=(ip, 50000), raise_server_exceptions=False)


def _headers(origin, content_type, cookie, csrf, extra):
    headers = {}
    if origin is not None:
        headers["Origin"] = origin
    if content_type is not None:
        headers["Content-Type"] = content_type
    if cookie is not None:
        headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie.value if isinstance(cookie, SessionToken) else cookie}"
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    headers.update(extra or {})
    return headers


def _post(client, path, body=None, *, origin=SHOP, content_type="application/json", cookie=None, csrf=None,
          raw=None, headers=None):
    content = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
    response = client.post(path, content=content, headers=_headers(origin, content_type, cookie, csrf, headers))
    client.cookies.clear()                     # cookie передаются только явно
    return response


def _get(client, path, *, cookie=None, origin=None):
    response = client.get(path, headers=_headers(origin, None, cookie, None, None))
    client.cookies.clear()
    return response


def _session_post(client, path, session, body=None, **kwargs):
    return _post(client, path, body, cookie=session, csrf=csrf_token(CSRF_KEY, session), **kwargs)


def _code(response):
    return response.json()["detail"]["code"]


def _cookies(response):
    return response.headers.get_list("set-cookie")


def _no_store(response):
    return response.headers.get("cache-control") == "no-store"


# ---------- выключенные аккаунты ----------

def test_disabled_accounts_have_no_routes_and_read_no_cookie(monkeypatch):
    def boom(*args):
        raise AssertionError("cookie прочитана при выключенных аккаунтах")
    monkeypatch.setattr(accounts.routes, "session_from_cookies", boom)
    client = _client(_app(settings=AccountSettings(enabled=False)))
    missing = _get(client, "/api/account/no-such-route")
    assert missing.status_code == 404
    session = SessionToken.new()
    for method, path in ALL_ROUTES:
        if method == "POST":
            response = _session_post(client, path, session, CREDENTIALS)
        else:
            response = _get(client, path, cookie=session)
        assert (response.status_code, response.content) == (404, missing.content), path
        assert response.headers.get("cache-control") == missing.headers.get("cache-control")
        assert not _cookies(response)


@pytest.mark.parametrize("method, path", ALL_ROUTES)
def test_accounts_disabled_in_database_look_like_a_missing_route(method, path):
    disabled = AccountServiceError("disabled")
    service = FakeService(**{name: disabled for name in _DEFAULTS})
    client = _client(_app(service))
    missing = _get(client, "/api/account/no-such-route")
    session = SessionToken.new()
    response = (_session_post(client, path, session, {**CREDENTIALS, "current_password": PASSWORD,
                                                      "new_password": NEW_PASSWORD})
                if method == "POST" else _get(client, path, cookie=session))
    assert (response.status_code, response.json()) == (404, missing.json()) == (404, {"detail": "Not Found"})
    assert _no_store(response) and _no_store(missing)


def test_router_factory_requires_service_and_trusted_origins():
    with pytest.raises(TypeError):
        build_account_router(None)
    with pytest.raises(TypeError):
        build_account_router(SETTINGS, None, TRUSTED)
    with pytest.raises(TypeError):
        build_account_router(SETTINGS, FakeService(), [SHOP])
    with pytest.raises(TypeError):
        build_account_router(SETTINGS, FakeService(), OriginAllowlist([]))
    assert build_account_router(AccountSettings(enabled=False)).routes == []


# ---------- регистрация и вход ----------

def test_register_sets_the_session_cookie_only_on_success():
    session = SessionToken.new()
    service = FakeService(register=SessionOutcome("created", session=session))
    response = _post(_client(_app(service)), REGISTER_PATH,
                     {"email": " Buyer@Example.TEST ", "password": PASSWORD, "customer_id": 1, "user_id": 2,
                      "is_admin": True})
    assert response.status_code == 201 and response.json() == {"result": "created"}
    assert service.calls == [("register", EMAIL, PASSWORD)]           # нормализованный email, лишние поля — мимо
    assert _cookies(response) == [session_cookie_header(session)]
    attributes = [part.strip() for part in _cookies(response)[0].split(";")]
    assert attributes[0] == f"{SESSION_COOKIE_NAME}={session.value}"
    assert set(attributes[1:]) == {"Max-Age=7776000", "Path=/", "Secure", "HttpOnly", "SameSite=Strict"}
    assert not any(part.lower().startswith("domain") for part in attributes)
    assert _no_store(response)


@pytest.mark.parametrize("outcome, status, code", [
    (SessionOutcome("email_taken"), 409, "email_taken"),
    (SessionOutcome("closed"), 403, "registration_closed"),
    (SessionOutcome("busy"), 503, "busy"),
])
def test_register_refusals(outcome, status, code):
    response = _post(_client(_app(FakeService(register=outcome))), REGISTER_PATH, CREDENTIALS)
    assert (response.status_code, _code(response)) == (status, code)
    assert not _cookies(response) and _no_store(response)
    if status == 503:
        assert response.headers["retry-after"] == "1"


def test_login_passes_previous_session_and_sets_a_new_cookie():
    old, new = SessionToken.new(), SessionToken.new()
    service = FakeService(login=SessionOutcome("ok", session=new))
    client = _client(_app(service))
    response = _post(client, LOGIN_PATH, CREDENTIALS, cookie=old)
    assert response.status_code == 200 and response.json() == {"result": "ok"}
    assert _cookies(response) == [session_cookie_header(new)]
    name, email, password, previous = service.calls[0]
    assert (name, email, password, previous.value) == ("login", EMAIL, PASSWORD, old.value)
    _post(client, LOGIN_PATH, CREDENTIALS, cookie="not-a-token")
    assert service.calls[1][3] is None                                  # неверная cookie в базу не идёт


def test_invalid_credentials_are_one_indistinguishable_401():
    """T-H1: неизвестный email, неверный пароль и отключённый аккаунт сервис возвращает одинаково (invalid)."""
    client = _client(_app(FakeService(login=SessionOutcome("invalid"))))
    responses = [_post(client, LOGIN_PATH, {"email": email, "password": password}, cookie=cookie)
                 for email, password, cookie in ((EMAIL, PASSWORD, None), ("nobody@example.test", PASSWORD, None),
                                                 (EMAIL, "Другой пароль 12345", SessionToken.new()))]
    assert {(r.status_code, r.content) for r in responses} == {(401, responses[0].content)}
    assert _code(responses[0]) == "invalid_credentials"
    assert not any(_cookies(r) for r in responses)                     # cookie запроса не удаляется


def test_login_throttled_by_the_database_returns_retry_after():
    response = _post(_client(_app(FakeService(login=SessionOutcome("throttled", retry_after_seconds=120)))),
                     LOGIN_PATH, CREDENTIALS)
    assert (response.status_code, _code(response), response.headers["retry-after"]) == (429, "rate_limited", "120")


@pytest.mark.parametrize("answer, status, code", [
    (AccountServiceError("retry", "commit_unknown"), 503, "service_unavailable"),     # C1: COMMIT не подтверждён
    (AccountServiceError("retry", "QueryCanceled:57014"), 503, "service_unavailable"),
    (AccountServiceError("overloaded"), 503, "busy"),
    (AccountServiceError("internal", "RaiseException:P0001"), 500, "internal_error"),
    (SessionOutcome("ok"), 500, "internal_error"),                                     # успех без сессии
    (SessionOutcome("created", session=SessionToken.new()), 500, "internal_error"),    # не результат входа
    ("ok", 500, "internal_error"),
    (RuntimeError("SELECT auth_login(...) QueryCanceled 57014"), 500, "internal_error"),  # ошибка в коде
])
def test_login_failures_never_set_a_cookie(answer, status, code):
    response = _post(_client(_app(FakeService(login=answer))), LOGIN_PATH, CREDENTIALS)
    assert (response.status_code, _code(response)) == (status, code)
    assert not _cookies(response) and _no_store(response)
    text = response.text + json.dumps(dict(response.headers))
    for leak in ("commit_unknown", "57014", "QueryCanceled", "P0001", "RaiseException"):
        assert leak not in text


def test_session_confirmed_after_unknown_commit_gets_a_cookie():
    """C2: сервис подтвердил сессию одним resolve после неизвестного исхода COMMIT — обычный успех."""
    session = SessionToken.new()
    response = _post(_client(_app(FakeService(login=SessionOutcome("ok", session=session)))), LOGIN_PATH,
                     CREDENTIALS)
    assert response.status_code == 200 and _cookies(response) == [session_cookie_header(session)]


# ---------- Origin, Content-Type, CSRF ----------

BAD_ORIGINS = [None, "https://evil.example.test", "null", SHOP + "/", "http://shop.example.test",
               "https://shop.example.test.evil.test", f"{SHOP}, https://evil.example.test"]
BAD_CONTENT_TYPES = [None, "text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x",
                     "application/jsonp"]


@pytest.mark.parametrize("path", [REGISTER_PATH, LOGIN_PATH, LOGOUT_PATH, LOGOUT_ALL_PATH, PASSWORD_PATH])
def test_cross_site_and_non_json_posts_are_refused_before_the_service(path):
    """T-H5, T-H7: отказ 403 csrf_failed, сервис не вызывается, cookie не удаляется."""
    service = FakeService()
    client = _client(_app(service))
    session = SessionToken.new()
    body = {**CREDENTIALS, "current_password": PASSWORD, "new_password": NEW_PASSWORD}
    for origin in BAD_ORIGINS:
        response = _session_post(client, path, session, body, origin=origin)
        assert (response.status_code, _code(response)) == (403, "csrf_failed"), origin
        assert not _cookies(response) and _no_store(response)
    for content_type in BAD_CONTENT_TYPES:
        response = _session_post(client, path, session, body, content_type=content_type)
        assert (response.status_code, _code(response)) == (403, "csrf_failed"), content_type
    assert service.calls == []


@pytest.mark.parametrize("path", [LOGOUT_ALL_PATH, PASSWORD_PATH])
def test_session_posts_require_the_csrf_token_of_this_session(path):
    service = FakeService()
    client = _client(_app(service))
    session = SessionToken.new()
    body = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    for cookie, token in ((session, None), (session, "A" * 43), (session, csrf_token(CSRF_KEY, SessionToken.new())),
                          (session, csrf_token(bytes(32), session)), (None, csrf_token(CSRF_KEY, session)),
                          ("not-a-token", csrf_token(CSRF_KEY, session))):
        response = _post(client, path, body, cookie=cookie, csrf=token)
        assert (response.status_code, _code(response)) == (403, "csrf_failed")
    token = csrf_token(CSRF_KEY, session)
    twice = client.post(path, content=json.dumps(body).encode(),
                        headers=[("Origin", SHOP), ("Content-Type", "application/json"),
                                 ("Cookie", f"{SESSION_COOKIE_NAME}={session.value}"),
                                 ("X-CSRF-Token", token), ("X-CSRF-Token", token)])
    assert (twice.status_code, _code(twice)) == (403, "csrf_failed")    # два заголовка — отказ
    assert _session_post(client, path, session, body).status_code == 200
    assert len(service.calls) == 1


def test_logout_needs_no_csrf_and_always_clears_the_cookie():
    """REV5 X6: Origin + JSON, без CSRF; после пройденной проверки cookie удаляется при любом исходе."""
    session = SessionToken.new()
    for answer, status in ((None, 200), (AccountServiceError("retry", "OperationalError:-"), 503),
                           (AccountServiceError("internal"), 500), (RuntimeError("bug"), 500)):
        service = FakeService(logout=answer)
        response = _post(_client(_app(service)), LOGOUT_PATH, cookie=session)
        assert response.status_code == status
        assert _cookies(response) == [clear_session_cookie_header()] and _no_store(response)
        assert [call[1].value for call in service.calls] == [session.value]
    service = FakeService()
    for cookie in (None, "garbage"):
        response = _post(_client(_app(service)), LOGOUT_PATH, cookie=cookie)
        assert response.status_code == 200 and _cookies(response) == [clear_session_cookie_header()]
    assert service.calls == []                                          # без сессии база не нужна


def test_logout_all_and_password_change():
    session, new = SessionToken.new(), SessionToken.new()
    service = FakeService(change_password=SessionOutcome("ok", session=new))
    client = _client(_app(service))
    response = _session_post(client, PASSWORD_PATH, session,
                             {"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert response.status_code == 200 and _cookies(response) == [session_cookie_header(new)]
    name, used, current, replacement = service.calls[-1]
    assert (name, used.value, current, replacement) == ("change_password", session.value, PASSWORD, NEW_PASSWORD)

    response = _session_post(client, LOGOUT_ALL_PATH, new)
    assert response.status_code == 200 and response.json() == {"result": "ok"}
    assert _cookies(response) == [clear_session_cookie_header()]


@pytest.mark.parametrize("outcome, status, code, clears", [
    (SessionOutcome("invalid"), 403, "invalid_password", False),
    (SessionOutcome("session_invalid"), 401, "session_invalid", True),
    (SessionOutcome("throttled", retry_after_seconds=30), 429, "rate_limited", False),
    (SessionOutcome("busy"), 503, "busy", False),
])
def test_password_change_refusals(outcome, status, code, clears):
    response = _session_post(_client(_app(FakeService(change_password=outcome))), PASSWORD_PATH, SessionToken.new(),
                             {"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert (response.status_code, _code(response)) == (status, code)
    assert _cookies(response) == ([clear_session_cookie_header()] if clears else [])


@pytest.mark.parametrize("result, status, code", [("session_invalid", 401, "session_invalid"), ("busy", 503, "busy"),
                                                  ("created", 500, "internal_error")])
def test_logout_all_refusals(result, status, code):
    response = _session_post(_client(_app(FakeService(logout_all=result))), LOGOUT_ALL_PATH, SessionToken.new())
    assert (response.status_code, _code(response)) == (status, code)
    assert _cookies(response) == ([clear_session_cookie_header()] if status == 401 else [])


# ---------- чтение: профиль и заказы ----------

def test_me_returns_profile_and_csrf_token_then_touches():
    session = SessionToken.new()
    service = FakeService(profile={**PROFILE, "password_hash": "$2a$12$x", "id": 5})
    response = _get(_client(_app(service)), ME_PATH, cookie=session)
    assert response.status_code == 200 and _no_store(response)
    assert response.json() == {**PROFILE, "csrf_token": csrf_token(CSRF_KEY, session)}   # лишнее не выходит
    assert service.names() == ["profile", "touch"]


def test_me_survives_a_failed_touch_and_needs_a_valid_session():
    session = SessionToken.new()
    assert _get(_client(_app(FakeService(touch=AccountServiceError("retry")))), ME_PATH,
                cookie=session).status_code == 200
    for service, cookie in ((FakeService(), None), (FakeService(), "garbage"), (FakeService(profile=None), session)):
        response = _get(_client(_app(service)), ME_PATH, cookie=cookie)
        assert (response.status_code, _code(response)) == (401, "session_invalid")
        assert _cookies(response) == [clear_session_cookie_header()]
        assert "touch" not in service.names()


def test_orders_list_only_known_fields():
    service = FakeService(orders=[{**ORDER, "access_token_sha256": "secret", "customer_id": 1}])
    response = _get(_client(_app(service)), ORDERS_PATH, cookie=SessionToken.new())
    assert response.status_code == 200 and response.json() == {"orders": [ORDER]}
    response = _get(_client(_app(FakeService(orders=None))), ORDERS_PATH, cookie=SessionToken.new())
    assert (response.status_code, _code(response)) == (401, "session_invalid")


def test_order_view_and_identical_404_for_foreign_missing_and_impossible_ids():
    session = SessionToken.new()
    service = FakeService(order=lambda s, number: {"order": {**ORDER, "id": number, "access_token": "x"},
                                                   "items": [{**ITEM, "secret": 1}]} if number == 7 else None)
    client = _client(_app(service))
    response = _get(client, ORDERS_PATH + "/7", cookie=session)
    assert response.status_code == 200 and response.json() == {"order": ORDER, "items": [ITEM]}

    not_found = [_get(client, ORDERS_PATH + "/" + text, cookie=session)
                 for text in ("8", "2147483647", "0", "-1", "2147483648", "abc", "1e3", "%D9%A3", "7.0",
                              "99999999999")]
    assert {(r.status_code, r.content) for r in not_found} == {(404, not_found[0].content)}
    assert _code(not_found[0]) == "order_not_found"                     # не 422 FastAPI с введённым значением
    assert [call[2] for call in service.calls if call[0] == "order"] == [7, 8, 2147483647]

    response = _get(_client(_app(FakeService(resolve=False))), ORDERS_PATH + "/8", cookie=session)
    assert (response.status_code, _code(response)) == (401, "session_invalid")


@pytest.mark.parametrize("path", [ME_PATH, ORDERS_PATH, ORDERS_PATH + "/7"])
def test_reads_without_a_session_do_not_reach_the_service(path):
    service = FakeService()
    response = _get(_client(_app(service)), path)
    assert (response.status_code, _code(response)) == (401, "session_invalid") and service.calls == []


# ---------- входные данные без отражения ----------

@pytest.mark.parametrize("raw", [
    b"", b"not json", b"[]", b'"text"', b"42", b"null", b"[" * 2000, b'{"email": "a@b", ', b"\xff\xfe\x00{",
])
def test_malformed_json_is_422_without_echo(raw):
    service = FakeService()
    response = _post(_client(_app(service)), LOGIN_PATH, raw=raw)
    assert (response.status_code, response.json()) == (422, {"detail": {"code": "invalid_request",
                                                                        "message": "Неверный формат запроса."}})
    assert service.calls == []


@pytest.mark.parametrize("body, code, field", [
    ({"password": PASSWORD}, "email_invalid", "email"),
    ({"email": 42, "password": PASSWORD}, "email_invalid", "email"),
    ({"email": ["x"], "password": PASSWORD}, "email_invalid", "email"),
    ({"email": "покупатель@пример.рф", "password": PASSWORD}, "email_invalid", "email"),
    ({"email": "released+1@invalid", "password": PASSWORD}, "email_reserved", "email"),
    ({"email": EMAIL}, "password_invalid", "password"),
    ({"email": EMAIL, "password": 1234567890}, "password_invalid", "password"),
    ({"email": EMAIL, "password": "short"}, "password_too_short", "password"),
    ({"email": EMAIL, "password": "Пароль\x00ёлки 2026"}, "password_forbidden_character", "password"),
    ({"email": EMAIL, "password": "x" * 129}, "password_too_long", "password"),
])
@pytest.mark.parametrize("path", [REGISTER_PATH, LOGIN_PATH])
def test_invalid_fields_are_422_with_code_and_field_only(path, body, code, field):
    service = FakeService()
    response = _post(_client(_app(service)), path, body)
    assert response.status_code == 422 and response.json()["detail"]["code"] == code
    assert response.json()["detail"]["field"] == field and set(response.json()["detail"]) == {"code", "message",
                                                                                               "field"}
    assert service.calls == []


def test_password_fields_are_checked_separately():
    session = SessionToken.new()
    client = _client(_app(FakeService()))
    response = _session_post(client, PASSWORD_PATH, session, {"current_password": PASSWORD, "new_password": "short"})
    assert response.json()["detail"] | {"message": None} == {"code": "password_too_short", "field": "new_password",
                                                              "message": None}
    response = _session_post(client, PASSWORD_PATH, session, {"new_password": NEW_PASSWORD})
    assert response.json()["detail"]["field"] == "current_password"


def test_no_response_contains_secrets_or_internal_details():
    session = SessionToken.new()
    marker_password = "Marker-Password-Секрет-2026"
    secrets = [marker_password, password_proof(PEPPER, marker_password), session.value, json.dumps(marker_password)]
    internals = ["SELECT", "auth_login", "57014", "QueryCanceled", "commit_unknown", "origin_forbidden",
                 "csrf_invalid", "content_type_not_json", "Traceback"]
    services = [FakeService(login=SessionOutcome("invalid")),
                FakeService(login=AccountServiceError("retry", "QueryCanceled:57014")),
                FakeService(login=AccountServiceError("internal", "RaiseException:P0001")),
                FakeService(login=RuntimeError(f"SELECT auth_login({marker_password})"))]
    responses = []
    for service in services:
        client = _client(_app(service))
        body = {"email": EMAIL, "password": marker_password}
        responses.append(_post(client, LOGIN_PATH, body, cookie=session))
        responses.append(_post(client, LOGIN_PATH, body, origin="https://evil.example.test", cookie=session))
        responses.append(_post(client, LOGIN_PATH, body, content_type="text/plain"))
        responses.append(_post(client, LOGIN_PATH, raw=json.dumps(body).encode()[:-1]))
        responses.append(_post(client, LOGIN_PATH, {"email": marker_password, "password": marker_password}))
        responses.append(_post(client, LOGIN_PATH, raw=json.dumps(body).encode() + b" " * 5000))
        responses.append(_post(client, PASSWORD_PATH, {"current_password": marker_password}, cookie=session,
                               csrf="B" * 43))
    for response in responses:
        assert response.status_code >= 400
        text = response.text + json.dumps(dict(response.headers), ensure_ascii=False)
        for value in secrets + internals:
            assert value not in text, (response.status_code, value)


# ---------- порядок защит и лимиты аккаунтов ----------

def test_origin_and_format_refusals_do_not_spend_the_registration_quota():
    service = FakeService()
    client = _client(_app(service))
    for origin in BAD_ORIGINS:
        assert _post(client, REGISTER_PATH, CREDENTIALS, origin=origin).status_code == 403
    for content_type in BAD_CONTENT_TYPES:
        assert _post(client, REGISTER_PATH, CREDENTIALS, content_type=content_type).status_code == 403
    for body in ({"email": "bad", "password": PASSWORD}, {"email": EMAIL, "password": "short"}):
        assert _post(client, REGISTER_PATH, body).status_code == 422
    assert _post(client, REGISTER_PATH, raw=b"{").status_code == 422
    assert service.calls == []
    assert _post(client, REGISTER_PATH, CREDENTIALS).status_code == 201       # квота 1/мин цела
    response = _post(client, REGISTER_PATH, CREDENTIALS)
    assert (response.status_code, _code(response)) == (429, "rate_limited")
    assert int(response.headers["retry-after"]) >= 1
    assert service.names() == ["register"] and _no_store(response)


def test_csrf_refusals_do_not_spend_the_password_quota():
    service = FakeService()
    client = _client(_app(service))
    session = SessionToken.new()
    body = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    for _ in range(10):
        assert _post(client, PASSWORD_PATH, body, cookie=session, csrf="C" * 43).status_code == 403
    assert [_session_post(client, PASSWORD_PATH, session, body).status_code for _ in range(6)] == [200] * 5 + [429]
    assert service.names() == ["change_password"] * 5


def test_login_limit_per_address_and_shared_process_budget():
    clock = _Clock()
    service = FakeService()
    app = _app(service, clock=clock)
    first = _client(app, "198.51.100.1")
    assert [_post(first, LOGIN_PATH, CREDENTIALS).status_code for _ in range(11)] == [200] * 10 + [429]
    # Межсайтовые запросы с сотни адресов не расходуют общий счётчик процесса.
    for n in range(100):
        assert _post(_client(app, f"203.0.113.{n}"), LOGIN_PATH, CREDENTIALS, origin="https://evil.test").status_code \
            == 403
    for n in range(50):                                                  # 10 + 50 = 60 на процесс
        path = LOGIN_PATH if n % 2 else REGISTER_PATH
        assert _post(_client(app, f"198.51.101.{n}"), path, CREDENTIALS).status_code in (200, 201)
    response = _post(_client(app, "198.51.102.1"), LOGIN_PATH, CREDENTIALS)
    assert (response.status_code, _code(response)) == (429, "rate_limited")
    assert len(service.calls) == 60
    clock.now += 61
    assert _post(_client(app, "198.51.102.1"), LOGIN_PATH, CREDENTIALS).status_code == 200


def test_account_limits_can_be_switched_off_with_the_global_flag():
    service = FakeService()
    client = _client(_app(service, rate_limits=False))
    assert [_post(client, REGISTER_PATH, CREDENTIALS).status_code for _ in range(3)] == [201] * 3


def test_body_over_4_kib_is_refused_before_the_service():
    service = FakeService()
    body = json.dumps({**CREDENTIALS, "padding": "x" * 4096}).encode()
    for stack in (True, False):                                          # middleware и проверка в маршруте
        response = _post(_client(_app(service, stack=stack)), LOGIN_PATH, raw=body)
        assert (response.status_code, _code(response)) == (413, "payload_too_large") and _no_store(response)
    assert service.calls == []


def test_every_route_response_is_no_store_even_without_the_middleware():
    session = SessionToken.new()
    client = _client(_app(FakeService(order=lambda s, n: {"order": ORDER, "items": []}), stack=False))
    responses = [_post(client, REGISTER_PATH, CREDENTIALS), _post(client, LOGIN_PATH, CREDENTIALS),
                 _post(client, LOGIN_PATH, CREDENTIALS, origin=None), _post(client, LOGIN_PATH, raw=b"{"),
                 _post(client, LOGOUT_PATH, cookie=session), _session_post(client, LOGOUT_ALL_PATH, session),
                 _get(client, ME_PATH, cookie=session), _get(client, ME_PATH),
                 _get(client, ORDERS_PATH, cookie=session), _get(client, ORDERS_PATH + "/7", cookie=session)]
    assert all(_no_store(r) for r in responses), [r.status_code for r in responses if not _no_store(r)]


def test_full_flow_uses_the_cookie_and_csrf_token_from_responses():
    service = FakeService()
    client = _client(_app(service))
    created = _post(client, REGISTER_PATH, CREDENTIALS)
    token = SessionToken(_cookies(created)[0].split(";")[0].split("=", 1)[1])
    me = _get(client, ME_PATH, cookie=token).json()
    assert me["csrf_token"] == csrf_token(CSRF_KEY, token)
    response = _post(client, LOGOUT_ALL_PATH, cookie=token, csrf=me["csrf_token"])
    assert response.status_code == 200
    assert [call[1].value for call in service.calls if call[0] == "logout_all"] == [token.value]
