"""PR6 этап 2e-3: привязка заказа к аккаунту (POST /api/v2/orders с cookie сессии) на настоящем PostgreSQL.

Маршруты — НАСТОЯЩИЕ из api.py (объекты APIRoute из api.app, файл не меняется), смонтированные на приложение,
собранное install_http_stack с включёнными аккаунтами (как в test_account_http_db.py): AccountResponseGuard,
CORS, лимиты, обработчик 422 и маршруты /api/account/*. Работают настоящие AccountOrders, create_order_with_access,
commerce.orders.create_order, customer_order_link_new, AccountService и PaymentService (платежи выключены).
api.app (аккаунты выключены, как по умолчанию) — образец прежнего ответа для гостевых запросов и API v1.

Безопасность запуска (решение владельцев):
  * только явно заданное окружение: PYTHON_DOTENV_DISABLED=1 (python-dotenv не читает .env), TEST_DB_NAME,
    DB_HOST/DB_PORT/DB_USER и пароль из скрытого ввода; без них модуль пропускается ДО импорта database.py;
  * защита tests/conftest.py: подключение только к TEST_DB_NAME; _lab_role_guard (test_account_http_db.py):
    в digital_store_pr6_stage1_test — только роль pr6_stage1_owner без прав суперпользователя;
  * всё — во временной схеме ds_mig_<10 hex> (make_schema/at_0005/accounts из test_migration_0005.py): миграции
    0000–0005, bcrypt cost 4, флаги auth_settings, покупатели, заказы — только там; схема удаляется в teardown
    make_schema (DROP SCHEMA … CASCADE только для имени, созданного этой фикстурой);
  * database.get_connection на время теста подменён фабрикой временной схемы (_OrderLab): каждое соединение
    получает search_path ТОЛЬКО из временной схемы и до первого SQL магазина проверяет current_database() и
    current_schemas(false) == [схема]. Иначе соединение закрывается, SQL магазина не выполняется
    (SchemaGuardError) — public при неверном search_path недостижим (test_wrong_search_path_is_refused_…);
  * ожидания ограничены: у каждого соединения lock_timeout 5 с, statement_timeout 30 с,
    idle_in_transaction_session_timeout 60 с (только параметры сеанса); у функций 0005 свой lock_timeout 2 с;
    потоки — Event.wait(10) и join(15); перед удалением схемы все соединения закрываются;
  * ролей, прав, баз и глобальных настроек тесты не создают и не меняют; чужие серверные процессы не завершают;
  * перец и ключ CSRF — тестовые значения в памяти (test_account_http_db.SETTINGS).
"""

import os

import pytest

# Значения, при которых python-dotenv 1.2 не читает .env (dotenv.main._load_dotenv_disabled).
_DOTENV_DISABLED_VALUES = {"1", "true", "t", "yes", "y"}

# Проверки — до импорта модулей, которые импортируют database.py (он вызывает load_dotenv() при импорте).
if os.environ.get("PYTHON_DOTENV_DISABLED", "").casefold() not in _DOTENV_DISABLED_VALUES:
    pytest.skip("привязка заказов на PostgreSQL: запуск только с PYTHON_DOTENV_DISABLED=1 (без чтения .env)",
                allow_module_level=True)
if "TEST_DB_NAME" not in os.environ:
    pytest.skip("привязка заказов на PostgreSQL: задайте TEST_DB_NAME явно", allow_module_level=True)

import hashlib  # noqa: E402
import itertools  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import secrets  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from psycopg import sql  # noqa: E402

import api  # noqa: E402
import commerce.orders  # noqa: E402
import database  # noqa: E402
import payments.service  # noqa: E402
from accounts import order_http  # noqa: E402  (не «import accounts…»: имя accounts — фикстура)
from accounts.order_http import AccountOrders  # noqa: E402
from accounts.request_guard import csrf_token  # noqa: E402
from accounts.service import AccountService  # noqa: E402
from accounts.session import SESSION_COOKIE_NAME, SessionToken  # noqa: E402
from http_stack import install_http_stack  # noqa: E402
from test_account_http_db import (  # noqa: E402,F401  (_lab_role_guard — фикстура модуля)
    CSRF_KEY, EMAIL, EVIL, OTHER, PASSWORD, SCHEMA_RE, SETTINGS, SHOP, _cleared, _lab_role_guard, _no_store, _session,
)
from test_account_service_db import _wait_new_connection  # noqa: E402
from test_account_web_config import _base  # noqa: E402
from test_migration_0005 import (  # noqa: E402,F401  (фикстуры)
    _Background, _call, _customer_id, _exec, _wait_blocked, accounts, at_0005, make_schema, product,
)
from test_migrations import _query, _run  # noqa: E402

SESSION_LIMITS = ("SET lock_timeout = '5s'", "SET statement_timeout = '30s'",
                  "SET idle_in_transaction_session_timeout = '60s'")
ORDER_TABLES = ("orders", "order_items", "order_access", "customer_orders")
CSRF_FAILED = {"detail": {"code": "csrf_failed", "message": "Запрос отклонён проверкой безопасности."}}
SESSION_INVALID = {"detail": {"code": "session_invalid", "message": "Нужно войти в аккаунт."}}
INVALID_REQUEST = {"detail": {"code": "invalid_request", "message": "Неверный формат запроса."}}
INTERNAL_ERROR = {"detail": {"code": "internal_error", "message": "Внутренняя ошибка."}}
SECRET_INPUT = "EXTRA-SECRET-VALUE"
_AUTO = object()


# ---------- соединения только временной схемы ----------

class SchemaGuardError(AssertionError):
    """Соединение не во временной схеме тестовой базы: SQL магазина на нём не выполняется."""


def _prepare(conn, schema, extra=()):
    """search_path — только временная схема; ограничения ожиданий; проверка базы и пути ДО SQL магазина.
    -> pid серверного процесса. Выполняются только SET и чтение current_database()/current_schemas()."""
    if not SCHEMA_RE.fullmatch(schema or ""):
        raise SchemaGuardError("имя схемы не из make_schema")
    with conn.cursor() as cur:
        cur.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        for statement in SESSION_LIMITS + tuple(extra):
            cur.execute(statement)
        cur.execute("SELECT current_database(), current_schemas(false), pg_backend_pid()")
        database_name, schemas, pid = cur.fetchone()
    conn.commit()
    if database_name != os.environ["TEST_DB_NAME"] or list(schemas) != [schema]:
        raise SchemaGuardError("соединение не во временной схеме тестовой базы")
    return pid


class _OrderLab:
    """Фабрика соединений временной схемы. connect — для приложения (подменяет database.get_connection);
    peer — для конкурирующих транзакций теста. opened — [(соединение, pid)] приложения в порядке открытия."""

    def __init__(self, owner, schema, connect_db):
        self.owner, self.schema = owner, schema
        self._connect_db = connect_db          # исходный database.get_connection: защита conftest
        self.opened, self.peers = [], []
        self.session_setup = []                # дополнительные SET для новых соединений приложения

    def _new(self, extra):
        conn = self._connect_db()
        try:
            return conn, _prepare(conn, self.schema, extra)
        except BaseException:
            conn.close()
            raise

    def connect(self):
        conn, pid = self._new(tuple(self.session_setup))
        self.opened.append((conn, pid))
        return conn

    def peer(self):
        conn, pid = self._new(())
        self.peers.append(conn)
        return conn, pid

    def close_all(self):
        for conn in [conn for conn, _ in self.opened] + self.peers:
            if not conn.closed:
                try:
                    conn.rollback()
                finally:
                    conn.close()


# ---------- стенд: настоящие маршруты api.py на стеке с включёнными аккаунтами ----------

def _enabled_app(lab):
    app = FastAPI()
    web = install_http_stack(app, limits={"rate_limits": True, "trusted_proxies": ()}, cors_options=_base([]),
                             account_settings=SETTINGS, env={"ACCOUNT_TRUSTED_ORIGINS": SHOP,
                                                             "CORS_ALLOWED_ORIGINS": ""},
                             account_service=lambda settings: AccountService(settings, connect=lab.connect))
    shop_routes = [route for route in api.app.routes if isinstance(route, APIRoute)]
    assert {"/api/v2/orders", "/orders", "/api/v2/orders/{order_id}/status"} <= {r.path for r in shop_routes}
    app.router.routes.extend(shop_routes)
    return app, web


class _Shop:
    def __init__(self, app, lab, create_calls):
        self.app, self.lab, self.owner, self.create_calls = app, lab, lab.owner, create_calls
        self._addresses = itertools.count(1)

    def request(self, method, path, *, app=None, body=None, raw=None, session=None, cookie_value=None, csrf=_AUTO,
                origin=SHOP, content_type="application/json", ip=None):
        headers = {}
        if origin is not None:
            headers["Origin"] = origin
        if content_type is not None and method == "POST":
            headers["Content-Type"] = content_type
        if cookie_value is not None or session is not None:
            headers["Cookie"] = f"{SESSION_COOKIE_NAME}={cookie_value if cookie_value is not None else session.value}"
        if csrf is _AUTO:
            csrf = csrf_token(CSRF_KEY, session) if session is not None else None
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        content = raw if raw is not None else (None if method == "GET" else json.dumps(body or {}).encode())
        number = next(self._addresses)                  # свой адрес — общие лимиты частоты не мешают сценариям
        client = TestClient(app or self.app, base_url="https://testserver", raise_server_exceptions=False,
                            client=(ip or f"198.18.{number // 250}.{number % 250 + 1}", 50000))
        return client.request(method, path, headers=headers, content=content)

    def order(self, product_id, session=None, **kwargs):
        body = kwargs.pop("body", {"items": [{"product_id": product_id, "quantity": 1}]})
        return self.request("POST", "/api/v2/orders", body=body, session=session, **kwargs)

    def get(self, path, session=None):
        return self.request("GET", path, session=session, origin=None)

    def session_post(self, path, session, body=None):
        return self.request("POST", path, body=body, session=session)

    def register(self, email):
        response = self.request("POST", "/api/account/register", body={"email": email, "password": PASSWORD})
        assert response.status_code == 201, response.text
        return _session(response)

    def status(self, order_id, access_token, **kwargs):
        return self.request("POST", f"/api/v2/orders/{order_id}/status", body={"access_token": access_token},
                            origin=None, **kwargs)

    def query(self, statement, params=None):
        return _query(self.owner, statement, params)

    def counts(self):
        return dict(zip(ORDER_TABLES, self.query(
            "SELECT " + ", ".join(f"(SELECT count(*) FROM {table})" for table in ORDER_TABLES))[0]))


@pytest.fixture
def shop(accounts):
    owner, schema = accounts
    assert SCHEMA_RE.fullmatch(schema), "работа только во временной схеме make_schema"
    assert commerce.orders.database is database and payments.service.database is database
    with owner.cursor() as cur:                         # наблюдатель и настройки: те же пределы ожиданий
        for statement in SESSION_LIMITS:
            cur.execute(statement)
        cur.execute("SELECT current_database(), current_schemas(false)")
        assert cur.fetchone() == (os.environ["TEST_DB_NAME"], [schema])
    owner.commit()
    lab = _OrderLab(owner, schema, database.get_connection)
    app, web = _enabled_app(lab)
    create_calls = []
    real_create = api.create_order_with_access

    def counting_create(items, after_access=None):
        create_calls.append("linked" if after_access is not None else "guest")
        return real_create(items, after_access=after_access)

    # Подмены снимаются при выходе из with — до teardown make_schema (удаление схемы — исходным соединением).
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(database, "get_connection", lab.connect)
        patch.setattr(api, "account_orders", AccountOrders(SETTINGS, web.trusted_origins))
        patch.setattr(api, "create_order_with_access", counting_create)
        try:
            yield _Shop(app, lab, create_calls)
        finally:
            lab.close_all()


def _created(response):
    body = response.json()
    return body["order"]["id"], body["access_token"]


def _guest_shape(response):
    """Тело гостевого ответа без того, что у каждого заказа своё (id, время, секрет)."""
    body = response.json()
    assert len(body.pop("access_token")) == 43
    body["order"].pop("id")
    body["order"].pop("created_at")
    return body


def _access_rows(shop, order_id, token):
    return shop.query("SELECT count(*) FROM order_access WHERE order_id = %s AND token_sha256 = %s",
                      (order_id, hashlib.sha256(token.encode("ascii")).digest()))[0][0]


def _linked_to(shop, order_id):
    rows = shop.query("SELECT customer_id FROM customer_orders WHERE order_id = %s", (order_id,))
    return rows[0][0] if rows else None


def _expiring_session(owner, email, seconds=1):
    """Сессия, истекающая через seconds секунд по часам сервера (запись во временной схеме)."""
    token = SessionToken.new()
    _call(owner, "INSERT INTO customer_sessions (customer_id, token_sha256, auth_epoch, idle_expires_at, expires_at) "
                 "SELECT id, %s, auth_epoch, clock_timestamp() + make_interval(secs => %s), "
                 "clock_timestamp() + make_interval(secs => %s) FROM customers WHERE email_norm = %s RETURNING id",
          (token.sha256, seconds, seconds, email))
    return token


def _assert_refused_without_trace(shop, response, before, status, body, *, cleared):
    assert (response.status_code, response.json()) == (status, body), response.text
    assert _no_store(response)
    assert _cleared(response) if cleared else not response.headers.get_list("set-cookie")
    assert shop.counts() == before                      # ни заказа, ни позиций, ни секрета, ни привязки


# ---------- 0. предохранитель search_path ----------

@pytest.mark.parametrize("wrong", ["DEFAULT", "public", "{schema}, public", "public, {schema}", "ds_mig_0000000000",
                                   "''"])
def test_wrong_search_path_is_refused_before_any_shop_sql(shop, product, wrong):
    """Неверный search_path (в том числе по умолчанию — "$user", public) не доходит до SQL магазина."""
    before = shop.counts()
    shop.lab.session_setup.append(f"SET search_path TO {wrong.format(schema=shop.lab.schema)}")
    opened = len(shop.lab.opened)
    with pytest.raises(SchemaGuardError):
        shop.lab.connect()
    guest = shop.order(product, origin=None)                     # гостевой путь: тот же отказ до SQL
    assert guest.status_code == 500 and shop.create_calls == ["guest"]
    assert len(shop.lab.opened) == opened and shop.counts() == before
    shop.lab.session_setup.clear()
    assert shop.order(product, origin=None).status_code == 201   # верный путь — заказ во временной схеме


# ---------- 1. гость при включённых аккаунтах ----------

def test_guest_order_with_accounts_enabled_keeps_the_old_contract(shop, product):
    before = shop.counts()
    guest = shop.order(product, origin=None)
    legacy = shop.order(product, origin=None, app=api.app)        # прежний стек: аккаунты выключены
    for response in (guest, legacy):
        assert response.status_code == 201 and set(response.json()) == {"order", "access_token"}
        assert "cache-control" not in response.headers and not response.headers.get_list("set-cookie")
    assert _guest_shape(guest) == _guest_shape(legacy) and set(guest.headers) == set(legacy.headers)
    for response in (guest, legacy):
        order_id, token = _created(response)
        assert _access_rows(shop, order_id, token) == 1 and _linked_to(shop, order_id) is None
        assert shop.status(order_id, token).status_code == 200
    after = shop.counts()
    assert after["orders"] == before["orders"] + 2 and after["order_access"] == before["order_access"] + 2
    assert after["customer_orders"] == before["customer_orders"] == 0
    assert shop.create_calls == ["guest", "guest"]


# ---------- 2. действительная сессия ----------

def test_valid_session_links_the_order_to_its_customer(shop, product):
    mine = shop.register(EMAIL)
    other = shop.register(OTHER)
    response = shop.order(product, mine)
    assert response.status_code == 201 and _no_store(response) and not response.headers.get_list("set-cookie")
    body = response.json()
    assert body.pop("account_linked") is True and set(body) == {"order", "access_token"}
    order_id, token = _created(response)
    assert _linked_to(shop, order_id) == _customer_id(shop.owner, EMAIL)
    assert _access_rows(shop, order_id, token) == 1
    assert shop.query("SELECT status, items_sealed FROM orders WHERE id = %s", (order_id,)) == [("pending", True)]
    assert shop.create_calls == ["linked"]

    listing = shop.get("/api/account/orders", mine)
    assert listing.status_code == 200 and [o["id"] for o in listing.json()["orders"]] == [order_id]
    assert shop.get(f"/api/account/orders/{order_id}", mine).status_code == 200
    assert shop.get("/api/account/orders", other).json() == {"orders": []}
    assert shop.get(f"/api/account/orders/{order_id}", other).status_code == 404
    # Гостевой секрет PR3 действует и для привязанного заказа — с cookie и без неё.
    for session in (None, mine):
        status = shop.status(order_id, token, session=session, csrf=None)
        assert status.status_code == 200 and status.json()["order"]["id"] == order_id
    assert shop.get("/api/account/me", mine).status_code == 200                 # сессия не тронута


def _without_order_identity(path, body):
    """Ответ status/delivery без того, что у каждого заказа своё (номер заказа и позиции, время)."""
    body = json.loads(json.dumps(body))
    if path == "status":
        body["order"].pop("id")
        body["order"].pop("created_at")
    elif path == "delivery":
        body.pop("order_id")
        for item in body["items"]:
            item.pop("order_item_id")
    return body


def test_t_h9d_linked_order_access_token_works_for_checkout_status_and_delivery(shop, product):
    """T-H9d (ADDENDUM §2): привязанный заказ — 201 и account_linked; его access_token действует для status
    и delivery с cookie и без неё (неверный секрет — 404), ответы как у гостевого заказа в том же состоянии
    (не оплачен: оплата и выдача ключей не имитируются). checkout при выключенных платежах отвечает 503 ещё
    до проверки секрета: здесь подтверждается только ответ как у гостевого заказа, не доступ по access_token.
    Cookie не заменяет access_token."""
    session = shop.register(EMAIL)
    linked = shop.order(product, session)
    assert linked.status_code == 201 and linked.json()["account_linked"] is True
    order_id, token = _created(linked)
    assert _linked_to(shop, order_id) == _customer_id(shop.owner, EMAIL)
    guest_id, guest_token = _created(shop.order(product, origin=None))
    assert _linked_to(shop, guest_id) is None

    def call(path, number, access_token, with_cookie=False):
        extra = {"session": session, "csrf": None} if with_cookie else {"origin": None}
        return shop.request("POST", f"/api/v2/orders/{number}/{path}", body={"access_token": access_token}, **extra)

    for path, expected_status in (("checkout", 503), ("status", 200), ("delivery", 200)):
        guest = call(path, guest_id, guest_token)
        assert guest.status_code == expected_status, (path, guest.text)
        for with_cookie in (False, True):
            response = call(path, order_id, token, with_cookie)
            assert response.status_code == expected_status, (path, with_cookie, response.text)
            assert _without_order_identity(path, response.json()) == _without_order_identity(path, guest.json())
            assert not response.headers.get_list("set-cookie")
    status = call("status", order_id, token).json()
    assert (status["order"]["id"], status["order"]["status"]) == (order_id, "pending")
    delivery = call("delivery", order_id, token).json()
    assert (delivery["order_id"], delivery["order_status"]) == (order_id, "pending")
    assert [item["delivery_status"] for item in delivery["items"]] == ["awaiting_payment"]
    assert all("digital_keys" not in item for item in delivery["items"])
    # Действительная cookie без верного секрета заказа ничего не открывает: 404, как у неверного секрета.
    wrong = secrets.token_urlsafe(32)
    for path in ("status", "delivery"):
        assert call(path, order_id, wrong, with_cookie=True).status_code == 404
        assert call(path, order_id, wrong).status_code == 404
    assert shop.query("SELECT status FROM orders WHERE id = %s", (order_id,)) == [("pending",)]
    assert shop.get("/api/account/me", session).status_code == 200


# ---------- 3–4. недействительная сессия: 401, cookie удаляется, всё откатывается ----------

def _unknown(shop):
    return SessionToken.new()


def _logged_out(shop):
    session = shop.register(EMAIL)
    assert _cleared(shop.session_post("/api/account/logout", session, {}))
    return session


def _logged_out_everywhere(shop):
    session = shop.register(EMAIL)
    assert shop.session_post("/api/account/logout-all", session).status_code == 200
    return session


def _expired(shop):
    shop.register(EMAIL)
    session = _expiring_session(shop.owner, EMAIL, seconds=1)
    time.sleep(1.3)                                                              # истекла по часам сервера
    return session


def _customer_disabled(shop):
    session = shop.register(EMAIL)
    assert _call(shop.owner, "SELECT customer_disable_core(%s, 'тест 2e-3', 'pytest')",
                 (_customer_id(shop.owner, EMAIL),)) == {"result": "ok"}
    return session


@pytest.mark.parametrize("make_session", [_unknown, _logged_out, _logged_out_everywhere, _expired, _customer_disabled],
                         ids=["неизвестная", "выход", "выход везде", "истекла", "покупатель отключён"])
def test_invalid_session_is_401_cookie_cleared_and_nothing_remains(shop, product, make_session):
    session = make_session(shop)
    before = shop.counts()
    response = shop.order(product, session)
    _assert_refused_without_trace(shop, response, before, 401, SESSION_INVALID, cleared=True)
    assert shop.create_calls == ["linked"]                                       # попытка была, гостевой нет


def test_accounts_disabled_in_database_refuse_with_cookie_and_roll_back(shop, product):
    session = shop.register(EMAIL)
    _run(shop.owner, "UPDATE auth_settings SET accounts_enabled = FALSE")       # только временная схема
    before = shop.counts()
    _assert_refused_without_trace(shop, shop.order(product, session), before, 401, SESSION_INVALID, cleared=True)
    guest = shop.order(product)                                                   # гости не зависят от флага
    assert guest.status_code == 201 and _linked_to(shop, _created(guest)[0]) is None
    _run(shop.owner, "UPDATE auth_settings SET accounts_enabled = TRUE")
    linked = shop.order(product, session)                                         # сессия до выключения снова действует
    assert linked.status_code == 201 and _linked_to(shop, _created(linked)[0]) == _customer_id(shop.owner, EMAIL)
    assert shop.create_calls == ["linked", "guest", "linked"]


# ---------- 5. Origin, Content-Type, CSRF и формат тела: до создания заказа ----------

def test_refusals_and_invalid_bodies_never_create_an_order(shop, product):
    session = shop.register(EMAIL)
    other = SessionToken.new()
    before, opened = shop.counts(), len(shop.lab.opened)
    refusals = {
        "чужой Origin": dict(origin=EVIL), "нет Origin": dict(origin=None), "Origin null": dict(origin="null"),
        "text/plain": dict(content_type="text/plain"), "нет Content-Type": dict(content_type=None),
        "нет CSRF": dict(csrf=None), "неверный CSRF": dict(csrf="A" * 43),
        "CSRF другой сессии": dict(csrf=csrf_token(CSRF_KEY, other)),
        "повреждённая cookie": dict(cookie_value="not-a-token", csrf="A" * 43),
        "неверный CSRF и тело": dict(csrf="A" * 43, raw=json.dumps({"items": [], "x": SECRET_INPUT}).encode()),
    }
    for name, options in refusals.items():
        raw = options.pop("raw", None)
        response = shop.order(product, session, raw=raw, **options)
        assert (response.status_code, response.json()) == (403, CSRF_FAILED), name
        assert _no_store(response) and not response.headers.get_list("set-cookie"), name
    invalid_bodies = {
        "лишнее поле": {"items": [{"product_id": product, "quantity": 1}], "customer_id": SECRET_INPUT},
        "пустой заказ": {"items": []},
        "строка вместо числа": {"items": [{"product_id": product, "quantity": SECRET_INPUT}]},
    }
    for name, body in invalid_bodies.items():
        response = shop.order(product, session, body=body)
        assert (response.status_code, response.json()) == (422, INVALID_REQUEST), name
        assert _no_store(response) and SECRET_INPUT not in response.text and session.value not in response.text
    broken = shop.order(product, session, raw=b'{"items": [' + SECRET_INPUT.encode())
    assert (broken.status_code, broken.json()) == (422, INVALID_REQUEST) and SECRET_INPUT not in broken.text
    too_large = shop.order(product, session, raw=b"x" * (17 * 1024))
    assert too_large.status_code == 413 and _no_store(too_large)
    limited = [shop.order(product, session, csrf=None, ip="203.0.113.77") for _ in range(11)]
    assert [r.status_code for r in limited] == [403] * 10 + [429] and all(_no_store(r) for r in limited)
    # Ни вызова создания заказа, ни соединения с базой, ни строк; сессия цела.
    assert shop.create_calls == [] and len(shop.lab.opened) == opened and shop.counts() == before
    assert shop.get("/api/account/me", session).status_code == 200


# ---------- 6. API v1 и гостевой checkout ----------

def test_v1_and_guest_checkout_are_unchanged_by_the_cookie(shop, product):
    session = shop.register(EMAIL)
    v1_product = _call(shop.owner, "INSERT INTO products (name, price, quantity, is_for_sale) "
                                   "VALUES ('pytest-v1', 999, 5, TRUE) RETURNING id")
    v1 = [shop.request("POST", "/orders", body={"product_id": v1_product}, origin=None),
          shop.request("POST", "/orders", body={"product_id": v1_product}, session=session),
          shop.request("POST", "/orders", body={"product_id": v1_product}, origin=None, app=api.app)]
    for response in v1:
        assert response.status_code == 200 and set(response.json()) == {"message", "order"}
        assert set(response.json()["order"]) == {"id", "product_id", "price", "status"}
        assert "cache-control" not in response.headers and not response.headers.get_list("set-cookie")
    v1_invalid = [shop.request("POST", "/orders", body={"product_id": SECRET_INPUT}, session=session),
                  shop.request("POST", "/orders", body={"product_id": SECRET_INPUT}, origin=None, app=api.app)]
    assert v1_invalid[0].status_code == v1_invalid[1].status_code == 422
    assert v1_invalid[0].json() == v1_invalid[1].json()                          # прежний 422 FastAPI

    order_id, token = _created(shop.order(product, origin=None))
    checkout = [shop.request("POST", f"/api/v2/orders/{order_id}/checkout", body={"access_token": token},
                             origin=None),
                shop.request("POST", f"/api/v2/orders/{order_id}/checkout", body={"access_token": token},
                             session=session),
                shop.request("POST", f"/api/v2/orders/{order_id}/checkout", body={"access_token": token},
                             origin=None, app=api.app)]
    assert {(r.status_code, r.content) for r in checkout} == {(503, checkout[0].content)}   # платежи выключены
    assert all("cache-control" not in r.headers for r in checkout)
    statuses = [shop.status(order_id, token), shop.status(order_id, token, session=session, csrf=None),
                shop.status(order_id, token, app=api.app)]
    assert {(r.status_code, r.content) for r in statuses} == {(200, statuses[0].content)}
    assert shop.counts()["customer_orders"] == 0


# ---------- 7. REPEATABLE READ ----------

def test_repeatable_read_refuses_without_a_partial_order(shop, product, caplog):
    session = shop.register(EMAIL)
    shop.lab.session_setup.append("SET default_transaction_isolation = 'repeatable read'")
    probe = shop.lab.connect()                                                   # настройка действует
    assert _query(probe, "SHOW transaction_isolation") == [("repeatable read",)]
    probe.close()
    before = shop.counts()
    with caplog.at_level(logging.INFO, logger="digital_store.accounts"):
        response = shop.order(product, session)
    _assert_refused_without_trace(shop, response, before, 500, INTERNAL_ERROR, cleared=False)
    assert "RaiseException" in caplog.text and session.value not in caplog.text
    shop.lab.session_setup.clear()
    assert shop.get("/api/account/me", session).status_code == 200              # сессия не тронута
    assert shop.order(product, session).status_code == 201                       # READ COMMITTED — привязка


# ---------- 8. конкурентность: оформление против «выйти везде» и отключения ----------

class _PausedLink:
    """Вместо link_new_order в accounts.order_http: настоящая привязка, затем пауза — транзакция заказа открыта,
    FOR SHARE на строке покупателя удерживается. После release — COMMIT или (fail) исключение и ROLLBACK."""

    def __init__(self, fail):
        self.real, self.fail = order_http.link_new_order, fail
        self.linked, self.release = threading.Event(), threading.Event()

    def __call__(self, cursor, session, order_id):
        self.real(cursor, session, order_id)
        self.linked.set()
        if not self.release.wait(10):
            raise TimeoutError("тест не отпустил транзакцию заказа")
        if self.fail:
            raise RuntimeError("сбой после привязки (тест)")


# Ядра администратора (обёртки admin_* требуют ролей — tests/test_customer_auth_privileges.py): отключение берёт
# строку покупателя FOR NO KEY UPDATE, освобождение email — FOR UPDATE (ADDENDUM2 §3.2).
RIVAL_CORE_SQL = {
    "disable": "SELECT customer_disable_core(%s, 'тест K9', 'pytest')",
    "release_email": "SELECT customer_release_email_core(%s, 'тест K10a', 'pytest')",
}


def _start_rival(shop, rival, session, customer_id):
    """«Выйти везде» (HTTP, AccountService), отключение покупателя или освобождение email (SQL ядра
    администратора) в потоке."""
    if rival == "logout_all":
        count = len(shop.lab.opened)
        step = _Background(lambda: shop.session_post("/api/account/logout-all", session))
        return step, _wait_new_connection(shop.lab, count)
    peer, pid = shop.lab.peer()
    return _Background(lambda: _call(peer, RIVAL_CORE_SQL[rival], (customer_id,))), pid


def _rival_succeeded(rival, step):
    if rival == "logout_all":
        assert step.result.status_code == 200 and _cleared(step.result)
    else:
        assert step.result == {"result": "ok"}


@pytest.mark.parametrize("finish", ["commit", "rollback"])
@pytest.mark.parametrize("rival", ["logout_all", "disable", "release_email"])
def test_order_holding_the_customer_lock_finishes_before_the_rival(shop, product, monkeypatch, rival, finish):
    """Заказ привязан и держит FOR SHARE; «выйти везде», отключение (FOR NO KEY UPDATE) или освобождение email
    (FOR UPDATE, K10b) ждёт. COMMIT заказа — привязка остаётся (зафиксирована до отзыва), затем сессия
    отозвана; ROLLBACK — ни заказа, ни привязки."""
    session = shop.register(EMAIL)
    customer_id = _customer_id(shop.owner, EMAIL)
    before = shop.counts()
    paused = _PausedLink(fail=finish == "rollback")
    monkeypatch.setattr(order_http, "link_new_order", paused)
    order = _Background(lambda: shop.order(product, session))
    try:
        assert paused.linked.wait(10), "заказ не дошёл до привязки"
        step, pid = _start_rival(shop, rival, session, customer_id)
        _wait_blocked(shop.owner, pid)                                           # ждёт FOR SHARE заказа
    finally:
        paused.release.set()
    order.join()
    step.join()
    assert order.error is None and step.error is None, (order.error, step.error)
    _rival_succeeded(rival, step)
    response = order.result
    if finish == "commit":
        assert response.status_code == 201 and response.json()["account_linked"] is True and _no_store(response)
        assert _linked_to(shop, _created(response)[0]) == customer_id
    else:
        _assert_refused_without_trace(shop, response, before, 500, INTERNAL_ERROR, cleared=False)
    assert shop.get("/api/account/me", session).status_code == 401              # отзыв/отключение — после


@pytest.mark.parametrize("finish", ["commit", "rollback"])
@pytest.mark.parametrize("rival", ["logout_all", "disable", "release_email"])
def test_order_waiting_for_the_rival_sees_its_outcome(shop, product, rival, finish):
    """Отзыв, отключение или освобождение email (K10a: HTTP-ответ покупателю) выполнено, но не зафиксировано;
    привязка ждёт FOR SHARE. COMMIT конкурента — повторная проверка после ожидания отказывает: 401 и полный
    откат; ROLLBACK — заказ привязан."""
    session = shop.register(EMAIL)
    customer_id = _customer_id(shop.owner, EMAIL)
    holder, _ = shop.lab.peer()
    if rival == "logout_all":
        assert _exec(holder, "SELECT auth_sessions_revoke_all(%s)", (session.sha256,)) == {"result": "ok"}
    else:
        assert _exec(holder, RIVAL_CORE_SQL[rival], (customer_id,)) == {"result": "ok"}
    before = shop.counts()
    count = len(shop.lab.opened)
    order = _Background(lambda: shop.order(product, session))
    try:
        _wait_blocked(shop.owner, _wait_new_connection(shop.lab, count))         # ждёт строку покупателя
    except BaseException:
        holder.rollback()
        raise
    if finish == "commit":
        holder.commit()
    else:
        holder.rollback()
    order.join()
    assert order.error is None, order.error
    response = order.result
    if finish == "commit":
        _assert_refused_without_trace(shop, response, before, 401, SESSION_INVALID, cleared=True)
    else:
        assert response.status_code == 201 and response.json()["account_linked"] is True
        assert _linked_to(shop, _created(response)[0]) == customer_id


# ---------- 9. ошибка после привязки, до COMMIT ----------

@pytest.mark.parametrize("failure", ["sql", "python"])
def test_failure_after_link_before_commit_rolls_everything_back(shop, product, monkeypatch, failure):
    session = shop.register(EMAIL)
    real_link, seen = order_http.link_new_order, []

    def link_then_fail(cursor, session_token, order_id):
        real_link(cursor, session_token, order_id)
        cursor.execute("SELECT count(*) FROM customer_orders WHERE order_id = %s", (order_id,))
        seen.append(cursor.fetchone()[0])                                        # привязка видна в транзакции
        if failure == "sql":
            cursor.execute("SELECT 1 / 0")                                       # настоящая ошибка базы
        raise RuntimeError("сбой после привязки (тест)")

    monkeypatch.setattr(order_http, "link_new_order", link_then_fail)
    before = shop.counts()
    _assert_refused_without_trace(shop, shop.order(product, session), before, 500, INTERNAL_ERROR, cleared=False)
    assert seen == [1]
    assert shop.get("/api/account/me", session).status_code == 200
