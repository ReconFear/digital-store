"""PR6 этап 2d-4: HTTP-маршруты аккаунтов с настоящим AccountService и функциями миграции 0005 на PostgreSQL.

Приложение — FastAPI + настоящий install_http_stack (http_stack.py) с теми же слоями, что в api.py; api.py не
импортируется. Сервис — accounts.service.AccountService на соединениях временной схемы. Проверяются сквозные
сценарии: регистрация → профиль → смена пароля → «выйти везде» → вход → выход; хранение только SHA-256 токенов;
F3, F4, T-H1; 429 из базы; свои и чужие заказы; Origin, CSRF, cookie и no-store в настоящих ответах.

Безопасность запуска (решение владельцев):
  * только явно заданное окружение: PYTHON_DOTENV_DISABLED=1 (python-dotenv не читает .env), TEST_DB_NAME,
    DB_HOST/DB_PORT/DB_USER и пароль из скрытого ввода; без них модуль пропускается ДО импорта database.py;
  * защита tests/conftest.py: подключение только к TEST_DB_NAME;
  * лаборатория digital_store_pr6_stage1_test — только под ролью pr6_stage1_owner, не суперпользователем
    (проверяется одним SELECT до создания схем); суперпользователь в других базах — только если он же задан
    в DS_TEST_SUPERUSER (одноразовый контейнер CI);
  * всё — во временных схемах ds_mig_<10 hex> (make_schema из test_migrations.py): миграции 0000–0005, bcrypt
    cost 4 и флаги auth_settings — только там; схема удаляется в teardown фикстуры даже при ошибке теста
    (DROP SCHEMA … CASCADE только для имени, созданного этой фикстурой). Таблицы public не затрагиваются;
  * ролей, прав и баз тесты не создают и не меняют; чужие серверные процессы не завершают;
  * перец и ключ CSRF — тестовые значения в памяти.
"""

import os
import re

import pytest

LAB_DB, LAB_ROLE = "digital_store_pr6_stage1_test", "pr6_stage1_owner"
# Значения, при которых python-dotenv 1.2 не читает .env (dotenv.main._load_dotenv_disabled).
_DOTENV_DISABLED_VALUES = {"1", "true", "t", "yes", "y"}

# Проверки — до импорта модулей, которые импортируют database.py (он вызывает load_dotenv() при импорте).
if os.environ.get("PYTHON_DOTENV_DISABLED", "").casefold() not in _DOTENV_DISABLED_VALUES:
    pytest.skip("HTTP-интеграция аккаунтов: запуск только с PYTHON_DOTENV_DISABLED=1 (без чтения .env)",
                allow_module_level=True)
if "TEST_DB_NAME" not in os.environ:
    pytest.skip("HTTP-интеграция аккаунтов: задайте TEST_DB_NAME явно", allow_module_level=True)

import hashlib  # noqa: E402
import json  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from accounts.config import AccountSettings, Pepper  # noqa: E402
from accounts.proof import password_proof  # noqa: E402
from accounts.request_guard import csrf_token  # noqa: E402
from accounts.service import AccountService  # noqa: E402
from accounts.session import (  # noqa: E402
    SESSION_COOKIE_NAME, SessionToken, clear_session_cookie_header, session_cookie_header,
)
from http_stack import install_http_stack  # noqa: E402
from test_account_service_db import _Lab, _tables_containing  # noqa: E402
from test_account_web_config import _base  # noqa: E402
from test_migration_0005 import (  # noqa: E402,F401  (фикстуры)
    _call, _link, _new_order, accounts, at_0005, make_schema, product,
)
from test_migrations import _query, _run  # noqa: E402

# Тестовые значения (не секреты): живут только в памяти процесса тестов.
PEPPER = Pepper(1, bytes(range(32)))
CSRF_KEY = bytes(range(64, 96))
SETTINGS = AccountSettings(enabled=True, pepper_current=PEPPER, csrf_key=CSRF_KEY)
SHOP = "https://shop.example.test"
EVIL = "https://evil.example.test"
EMAIL, OTHER, THIRD = "buyer@example.test", "other@example.test", "third@example.test"
PASSWORD = "Пароль ёлки 2026"
NEW_PASSWORD = "Новый пароль ёлки 2027"
WRONG_PASSWORD = "Неверный пароль 2026"
SCHEMA_RE = re.compile(r"ds_mig_[0-9a-f]{10}")
NOT_FOUND = b'{"detail":"Not Found"}'


# ---------- предохранители запуска ----------

@pytest.fixture(scope="module", autouse=True)
def _lab_role_guard():
    """До создания схем: та база и та роль. Одно чтение каталога, без изменений."""
    import database
    conn = database.get_connection()
    try:
        rows = _query(conn, "SELECT current_database(), current_user, r.rolsuper FROM pg_roles r "
                            "WHERE r.rolname = current_user")
    finally:
        conn.close()
    assert len(rows) == 1, "роль текущего соединения не найдена в pg_roles"
    database_name, role, superuser = rows[0]
    if database_name == LAB_DB and (role != LAB_ROLE or superuser):
        pytest.fail(f"лаборатория {LAB_DB}: только роль {LAB_ROLE} без прав суперпользователя")
    if superuser and os.environ.get("DS_TEST_SUPERUSER") != role:
        pytest.fail("суперпользователь разрешён только в одноразовом контейнере CI (DS_TEST_SUPERUSER)")


# ---------- стенд: настоящий стек HTTP на временной схеме ----------

class _Shop:
    def __init__(self, app, lab):
        self.app, self.lab, self.owner = app, lab, lab.owner

    def request(self, method, path, *, body=None, raw=None, cookie=None, csrf=None, origin=SHOP,
                content_type="application/json", ip="198.51.100.10"):
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        if content_type is not None and method == "POST":
            headers["Content-Type"] = content_type
        if cookie is not None:
            headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie.value}"
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        content = raw if raw is not None else (None if method == "GET" else json.dumps(body or {}).encode())
        client = TestClient(self.app, base_url="https://testserver", client=(ip, 50000),
                            raise_server_exceptions=False)
        return client.request(method, path, headers=headers, content=content)

    def post(self, path, body=None, **kwargs):
        return self.request("POST", path, body=body, **kwargs)

    def get(self, path, **kwargs):
        return self.request("GET", path, origin=kwargs.pop("origin", None), **kwargs)

    def session_post(self, path, session, body=None, **kwargs):
        return self.post(path, body, cookie=session, csrf=csrf_token(CSRF_KEY, session), **kwargs)

    def register(self, email, password=PASSWORD, ip="198.51.100.10"):
        response = self.post("/api/account/register", {"email": email, "password": password}, ip=ip)
        assert response.status_code == 201, response.text
        return _session(response)

    def query(self, sql, params=None):
        return _query(self.owner, sql, params)


def _session(response):
    """Токен из Set-Cookie ответа (атрибуты проверяются отдельно)."""
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 1 and cookies[0].startswith(f"{SESSION_COOKIE_NAME}=")
    token = SessionToken(cookies[0].split(";", 1)[0].split("=", 1)[1])
    assert cookies == [session_cookie_header(token)]                   # __Host-, Secure, HttpOnly, Strict, Path=/
    return token


def _code(response):
    return response.json()["detail"]["code"]


def _no_store(response):
    return response.headers.get("cache-control") == "no-store"


def _cleared(response):
    return response.headers.get_list("set-cookie") == [clear_session_cookie_header()]


@pytest.fixture
def shop(accounts):
    owner, schema = accounts
    assert SCHEMA_RE.fullmatch(schema), "работа только во временной схеме make_schema"
    lab = _Lab(owner, schema)
    app = FastAPI()
    install_http_stack(app, limits={"rate_limits": True, "trusted_proxies": ()}, cors_options=_base([]),
                       account_settings=SETTINGS, env={"ACCOUNT_TRUSTED_ORIGINS": SHOP, "CORS_ALLOWED_ORIGINS": ""},
                       account_service=lambda settings: AccountService(settings, connect=lab.connect))
    yield _Shop(app, lab)
    for conn, _ in lab.opened:                                         # до удаления схемы (make_schema)
        if not conn.closed:
            conn.close()


# ---------- 1–2. сквозной сценарий и хранение только хэшей ----------

def test_register_profile_password_revoke_login_logout(shop):
    created = shop.post("/api/account/register", {"email": " Buyer@Example.TEST ", "password": PASSWORD})
    assert created.status_code == 201 and created.json() == {"result": "created"} and _no_store(created)
    first = _session(created)
    assert shop.query("SELECT email_norm, left(password_hash, 7), auth_epoch FROM customers") == \
        [(EMAIL, "$2a$04$", 1)]

    me = shop.get("/api/account/me", cookie=first)
    assert me.status_code == 200 and _no_store(me)
    assert me.json()["email"] == EMAIL and me.json()["csrf_token"] == csrf_token(CSRF_KEY, first)

    changed = shop.session_post("/api/account/password", first,
                                {"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert changed.status_code == 200 and changed.json() == {"result": "ok"}
    second = _session(changed)
    assert second.value != first.value
    stale = shop.get("/api/account/me", cookie=first)
    assert (stale.status_code, _code(stale)) == (401, "session_invalid") and _cleared(stale)
    assert shop.get("/api/account/me", cookie=second).status_code == 200
    assert shop.query("SELECT auth_epoch FROM customers") == [(2,)]

    revoked = shop.session_post("/api/account/logout-all", second)
    assert revoked.status_code == 200 and _cleared(revoked)
    assert shop.get("/api/account/me", cookie=second).status_code == 401
    assert shop.query("SELECT count(*) FROM customer_sessions WHERE revoked_at IS NULL") == [(0,)]
    assert shop.query("SELECT auth_epoch FROM customers") == [(3,)]

    assert shop.post("/api/account/login", {"email": EMAIL, "password": PASSWORD}).status_code == 401
    logged_in = shop.post("/api/account/login", {"email": EMAIL, "password": NEW_PASSWORD})
    assert logged_in.status_code == 200
    third = _session(logged_in)
    assert shop.get("/api/account/me", cookie=third).status_code == 200

    logged_out = shop.post("/api/account/logout", cookie=third)
    assert logged_out.status_code == 200 and _cleared(logged_out) and _no_store(logged_out)
    assert shop.get("/api/account/me", cookie=third).status_code == 401
    assert shop.query("SELECT revoked_at IS NOT NULL FROM customer_sessions WHERE token_sha256 = %s",
                      (third.sha256,)) == [(True,)]
    assert dict(shop.query("SELECT action, count(*) FROM audit_log WHERE action LIKE 'auth.%%' GROUP BY action")) \
        == {"auth.register": 1, "auth.password_change": 1, "auth.sessions_revoke_all": 1, "auth.login": 1}


def test_database_keeps_only_token_hashes_and_login_revokes_the_previous_session(shop):
    registered = shop.register(EMAIL)
    other = shop.register(OTHER, ip="198.51.100.11")
    login = shop.post("/api/account/login", {"email": EMAIL, "password": PASSWORD}, cookie=registered)
    current = _session(login)
    # Фиксация сессии: прежняя сессия того же покупателя отозвана; сессия чужого покупателя — нет.
    assert shop.get("/api/account/me", cookie=registered).status_code == 401
    shop.post("/api/account/login", {"email": EMAIL, "password": PASSWORD}, cookie=other)
    assert shop.get("/api/account/me", cookie=other).status_code == 200

    rows = shop.query("SELECT token_sha256, octet_length(token_sha256) FROM customer_sessions")
    assert all(length == 32 for _, length in rows)
    stored = {bytes(token) for token, _ in rows}
    for token in (registered, other, current):
        assert hashlib.sha256(token.value.encode("ascii")).digest() == token.sha256 and token.sha256 in stored
        assert _tables_containing(shop.owner, token.value) == []          # сам токен нигде не хранится
    for secret in (PASSWORD, password_proof(PEPPER, PASSWORD)):
        assert _tables_containing(shop.owner, secret) == []
    assert _tables_containing(shop.owner, EMAIL) == ["customers"]


# ---------- 3–4. флаги в базе ----------

def test_f3_accounts_disabled_in_database_look_missing_and_touch_nothing(shop):
    session = shop.register(EMAIL)
    _run(shop.owner, "UPDATE auth_settings SET accounts_enabled = FALSE")       # только временная схема
    before = (shop.query("SELECT count(*) FROM customers"), shop.query("SELECT count(*) FROM auth_throttle"),
              shop.query("SELECT count(*) FROM audit_log"))
    missing = shop.get("/api/account/no-such-route")
    for response in (shop.post("/api/account/register", {"email": OTHER, "password": PASSWORD}, ip="198.51.100.12"),
                     shop.post("/api/account/login", {"email": EMAIL, "password": PASSWORD})):
        assert (response.status_code, response.content) == (404, missing.content) == (404, NOT_FOUND)
        assert _no_store(response) and not response.headers.get_list("set-cookie")
    after = (shop.query("SELECT count(*) FROM customers"), shop.query("SELECT count(*) FROM auth_throttle"),
             shop.query("SELECT count(*) FROM audit_log"))
    assert after == before                                              # ни покупателя, ни учёта попыток, ни bcrypt
    # Сессия на время выключения недействительна (как и в базе — auth_session_resolve).
    assert shop.get("/api/account/me", cookie=session).status_code == 401


def test_f4_closed_registration_is_403_but_login_works(shop):
    shop.register(EMAIL)
    _run(shop.owner, "UPDATE auth_settings SET registration_open = FALSE")      # только временная схема
    closed = shop.post("/api/account/register", {"email": OTHER, "password": PASSWORD}, ip="198.51.100.13")
    assert (closed.status_code, _code(closed)) == (403, "registration_closed") and _no_store(closed)
    assert not closed.headers.get_list("set-cookie")
    assert shop.query("SELECT email_norm FROM customers") == [(EMAIL,)]
    assert shop.post("/api/account/login", {"email": EMAIL, "password": PASSWORD}).status_code == 200


# ---------- 5–6. неверные учётные данные и ограничение попыток ----------

def test_t_h1_unknown_email_wrong_password_and_disabled_customer_are_one_401(shop):
    shop.register(EMAIL)
    shop.register(OTHER, ip="198.51.100.14")
    disabled_id = shop.query("SELECT id FROM customers WHERE email_norm = %s", (OTHER,))[0][0]
    # Ядро функции администратора (SECURITY INVOKER) во временной схеме; _call фиксирует транзакцию.
    assert _call(shop.owner, "SELECT customer_disable_core(%s, 'тест T-H1', 'pytest')", (disabled_id,)) == \
        {"result": "ok"}
    responses = [shop.post("/api/account/login", {"email": email, "password": password}, ip=ip)
                 for email, password, ip in (("nobody@example.test", PASSWORD, "198.51.100.21"),
                                             (EMAIL, WRONG_PASSWORD, "198.51.100.22"),
                                             (OTHER, PASSWORD, "198.51.100.23"))]
    assert {(r.status_code, r.content) for r in responses} == {(401, responses[0].content)}
    assert _code(responses[0]) == "invalid_credentials"
    assert all(_no_store(r) and not r.headers.get_list("set-cookie") for r in responses)


def test_database_throttle_is_429_with_retry_after(shop):
    shop.register(EMAIL)
    _run(shop.owner, "UPDATE auth_settings SET login_limit = 2")                # только временная схема
    for _ in range(2):
        assert shop.post("/api/account/login", {"email": EMAIL, "password": WRONG_PASSWORD}).status_code == 401
    throttled = shop.post("/api/account/login", {"email": EMAIL, "password": PASSWORD})
    assert (throttled.status_code, _code(throttled)) == (429, "rate_limited") and _no_store(throttled)
    # Срок из базы (окно 15 мин), а не из лимита маршрута (≤ 60 с): ответ пришёл от auth_login.
    assert 60 < int(throttled.headers["retry-after"]) <= 15 * 60
    assert not throttled.headers.get_list("set-cookie")
    assert shop.query("SELECT attempts FROM auth_throttle WHERE scope = 1") == [(3,)]


# ---------- 7. заказы ----------

def test_own_orders_200_foreign_and_missing_are_one_404(shop, product):
    mine = shop.register(EMAIL)
    foreign = shop.register(OTHER, ip="198.51.100.15")
    with shop.owner.cursor() as cur:                                    # привязка — этап 2e; здесь SQL-помощник
        order_id = _new_order(cur, product)
        assert _link(cur, mine.sha256, order_id) == {"result": "linked"}
    shop.owner.commit()

    listing = shop.get("/api/account/orders", cookie=mine)
    assert listing.status_code == 200 and [o["id"] for o in listing.json()["orders"]] == [order_id]
    detail = shop.get(f"/api/account/orders/{order_id}", cookie=mine)
    assert detail.status_code == 200 and detail.json()["order"]["id"] == order_id
    assert len(detail.json()["items"]) == 1 and "access" not in detail.text and _no_store(detail)

    assert shop.get("/api/account/orders", cookie=foreign).json() == {"orders": []}
    not_found = [shop.get(f"/api/account/orders/{order_id}", cookie=foreign),
                 shop.get(f"/api/account/orders/{order_id + 1_000_000}", cookie=mine),
                 shop.get("/api/account/orders/abc", cookie=mine)]
    assert {(r.status_code, r.content) for r in not_found} == {(404, not_found[0].content)}
    assert _code(not_found[0]) == "order_not_found"
    assert shop.get(f"/api/account/orders/{order_id}", cookie=SessionToken.new()).status_code == 401


# ---------- 8. Origin, CSRF, cookie и no-store в настоящих ответах ----------

def test_origin_csrf_and_no_store_refusals_never_reach_the_database(shop):
    session = shop.register(EMAIL)
    throttle_before = shop.query("SELECT count(*) FROM auth_throttle")
    for origin in (EVIL, None, "null"):
        refused = shop.post("/api/account/login", {"email": EMAIL, "password": WRONG_PASSWORD}, origin=origin)
        assert (refused.status_code, _code(refused)) == (403, "csrf_failed") and _no_store(refused)
    assert shop.post("/api/account/register", {"email": OTHER, "password": PASSWORD}, content_type="text/plain",
                     ip="198.51.100.16").status_code == 403
    assert shop.query("SELECT count(*) FROM auth_throttle") == throttle_before    # попытки не учтены
    assert shop.query("SELECT count(*) FROM customers") == [(1,)]

    other_session = SessionToken.new()
    for csrf in (None, "A" * 43, csrf_token(CSRF_KEY, other_session)):
        refused = shop.post("/api/account/logout-all", cookie=session, csrf=csrf)
        assert (refused.status_code, _code(refused)) == (403, "csrf_failed")
        refused = shop.post("/api/account/password", {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                            cookie=session, csrf=csrf)
        assert refused.status_code == 403
    cross_site_logout = shop.post("/api/account/logout", cookie=session, origin=EVIL)
    assert cross_site_logout.status_code == 403 and not cross_site_logout.headers.get_list("set-cookie")
    assert shop.get("/api/account/me", cookie=session).status_code == 200           # сессия цела
    assert shop.query("SELECT auth_epoch FROM customers") == [(1,)]                # пароль и эпоха не менялись

    too_large = shop.post("/api/account/login", raw=b"x" * 5000)
    assert too_large.status_code == 413 and _no_store(too_large)
    unknown = shop.get("/api/account/no-such-route")
    assert unknown.status_code == 404 and _no_store(unknown)
