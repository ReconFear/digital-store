"""Права PostgreSQL (deploy/db_roles.py): негативные и позитивные проверки под настоящими ролями.

ВНИМАНИЕ: тесты СОЗДАЮТ временные роли в кластере PostgreSQL (имена dst_<случайно>_*)
и удаляют их в конце. Существующие роли не меняются. Поэтому тесты запускаются только
по явному согласию:  DS_TEST_CREATE_ROLES=1  и при праве CREATEROLE у тестовой роли.
Без этого они пропускаются (skip) с объяснением.

Схема магазина — временная (make_schema), владелец — тестовая роль (в роли «мигратора»).
"""

import os
import secrets
import uuid

import pytest

import database
import migrate
import test_migrations
from deploy import db_roles
from payment_testkit import SHOP_ID, SECRET, notification, yookassa  # noqa: F401
from payments.access import create_order_with_access
from payments.config import PaymentSettings
from payments.service import PaymentService
from payments.yookassa import YooKassaProvider
from test_migrations import _quiet, _run

make_schema = test_migrations.make_schema


def _connect_as(role, password, schema):
    conn = database.psycopg.connect(host=os.getenv("DB_HOST"), port=os.getenv("DB_PORT"),
                                    dbname=os.getenv("DB_NAME"), user=role, password=password)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
    conn.commit()
    return conn


class _Role:
    def __init__(self, name, password, schema):
        self.name, self.password, self.schema = name, password, schema

    def connect(self):
        return _connect_as(self.name, self.password, self.schema)

    def run(self, sql, params=None):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall() if cur.description else None
            conn.commit()
            return rows
        finally:
            conn.close()

    def refused(self, sql, params=None, match="permission denied|must be owner|запрещ|нельзя"):
        with pytest.raises(Exception, match=match):
            self.run(sql, params)


@pytest.fixture
def shop_roles(make_schema):
    if os.getenv("DS_TEST_CREATE_ROLES") != "1":
        pytest.skip("тесты прав создают временные роли PostgreSQL: включите DS_TEST_CREATE_ROLES=1")
    with database.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname = current_user")
            if not cur.fetchone()[0]:
                pytest.skip("у тестовой роли нет CREATEROLE — тесты прав пропущены")

    conn, schema = make_schema()
    migrate.apply_migrations(conn, log=_quiet)
    prefix = f"dst_{uuid.uuid4().hex[:8]}"
    names = db_roles.role_names(prefix)
    passwords = {kind: secrets.token_urlsafe(16) for kind in ("app", "worker", "admin_user")}
    admin_user = f"{prefix}_admin_user"
    _run(conn, db_roles.create_roles_sql(prefix))
    for kind in ("app", "worker"):
        _run(conn, f"ALTER ROLE {names[kind]} PASSWORD '{passwords[kind]}'")
    _run(conn, f"CREATE ROLE {admin_user} LOGIN PASSWORD '{passwords['admin_user']}' IN ROLE {names['admin']}")
    _run(conn, db_roles.grants_sql(schema, prefix))
    _run(conn, "INSERT INTO products (name, price_minor, quantity, is_for_sale) VALUES ('A', 10000, 5, TRUE)")

    roles = {
        "owner": conn, "schema": schema,
        "app": _Role(names["app"], passwords["app"], schema),
        "worker": _Role(names["worker"], passwords["worker"], schema),
        "admin": _Role(admin_user, passwords["admin_user"], schema),
    }
    try:
        roles["app"].run("SELECT 1")
    except Exception as exc:  # noqa: BLE001 — окружение не пускает новые роли (pg_hba, CONNECT)
        _drop_roles(conn, schema, list(names.values()) + [admin_user])
        pytest.skip(f"не удалось подключиться новой ролью: {type(exc).__name__}")
    yield roles
    _drop_roles(conn, schema, list(names.values()) + [admin_user])


def _drop_roles(conn, schema, roles):
    # Права ролей есть только на объекты временной схемы: удаляем схему, затем роли.
    # (DROP OWNED BY требует членства в роли, которого у создателя в PG16+ по умолчанию нет.)
    conn.rollback()
    _run(conn, f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    for role in reversed(roles):
        _run(conn, f"DROP ROLE IF EXISTS {role}")


@pytest.fixture
def as_role(monkeypatch):
    """Подменить database.get_connection подключением под заданной ролью."""
    def use(role):
        monkeypatch.setattr(database, "get_connection", role.connect)
    return use


def _order_via_app(shop_roles, as_role):
    as_role(shop_roles["app"])
    order, token = create_order_with_access([(1, 1)])
    return order, token


# ---------- 11. приложение не может изменить статус и историю ----------

@pytest.mark.parametrize("sql", [
    "UPDATE orders SET status = 'paid', paid_at = now() WHERE id = 1",
    "UPDATE orders SET status = 'cancelled' WHERE id = 1",
    "UPDATE orders SET total_minor = 1 WHERE id = 1",
    "UPDATE products SET quantity = 1000 WHERE id = 1",
    "UPDATE products SET price_minor = 1 WHERE id = 1",
    "DELETE FROM orders WHERE id = 1",
    "DELETE FROM order_items",
    "TRUNCATE orders CASCADE",
    "INSERT INTO money_operations (kind, payment_id, order_id, provider, provider_operation_id, amount_minor, "
    "currency, provider_test) VALUES ('payment', 1, 1, 'yookassa', 'x', 1, 'RUB', TRUE)",
    "INSERT INTO payments (order_id, provider, idempotence_key, amount_minor, currency) "
    "VALUES (1, 'yookassa', gen_random_uuid(), 10000, 'RUB')",
    "UPDATE payments SET status = 'succeeded'",
    "UPDATE fulfillments SET status = 'fulfilled'",
    "SELECT apply_paid_order(1)",
    "SELECT payment_record_provider_state(1, 'x', 'succeeded', 10000, 'RUB', TRUE, TRUE, NULL)",
    "SELECT admin_retry_paid_order(1)",
    "SELECT request_refund(1, 1, 'x')",
    "SELECT * FROM money_operations",
    # rev2: секреты заказов, платежи, возвраты, исполнение и очередь — только через функции
    "SELECT token_sha256 FROM order_access",
    "SELECT * FROM payments",
    "SELECT * FROM refunds",
    "SELECT * FROM fulfillments",
    "SELECT * FROM provider_events",
    "INSERT INTO order_access (order_id, token_sha256) VALUES (1, decode(repeat('ab', 32), 'hex'))",
    "INSERT INTO provider_events (provider, event_type, object_id) VALUES ('yookassa', 'payment.succeeded', 'x')",
    "SELECT payment_attempt_bind(1, 'fake-payment', 'https://evil.example/', TRUE)",
    "SELECT payment_attempt_mark(1, 'failed', 'x')",
    "SELECT payment_attempt_touch(1, NULL)",
    "SELECT admin_resolve_payment(1, 'not_paid_confirmed', 'x')",
    "SELECT * FROM audit_log",
    "ALTER TABLE orders DISABLE TRIGGER orders_guard_status",
    "CREATE OR REPLACE FUNCTION apply_paid_order(p_order_id INTEGER) RETURNS JSONB "
    "LANGUAGE sql AS 'SELECT NULL::jsonb'",
    "CREATE TABLE evil (id INT)",
])
def test_app_role_cannot_touch_money_status_or_history(shop_roles, as_role, sql):
    _order_via_app(shop_roles, as_role)
    shop_roles["app"].refused(sql)
    owner = shop_roles["owner"]
    with owner.cursor() as cur:
        cur.execute("SELECT status, quantity FROM orders o, products p WHERE o.id = 1 AND p.id = 1")
        assert cur.fetchone() == ("pending", 5)
    owner.rollback()


def test_app_role_cannot_reopen_sealed_order(shop_roles, as_role):
    _order_via_app(shop_roles, as_role)
    shop_roles["app"].refused("UPDATE orders SET items_sealed = FALSE WHERE id = 1", match="снять нельзя|нельзя")


@pytest.mark.parametrize("sql", [
    "UPDATE orders SET status = 'paid' WHERE id = 1",
    "UPDATE products SET quantity = 0",
    "INSERT INTO order_items (order_id, product_id, product_name, product_type, fulfillment_type, quantity, "
    "unit_price_minor, currency, line_total_minor) VALUES (1, 1, 'x', 'other', 'manual', 1, 0, 'RUB', 0)",
    "SELECT apply_paid_order(1)",
    "SELECT payment_attempt_start(1, decode(repeat('00', 32), 'hex'), 'yookassa', gen_random_uuid())",
    "DELETE FROM provider_events",
    "INSERT INTO provider_events (provider, event_type, object_id) VALUES ('yookassa', 'payment.succeeded', 'x')",
    "SELECT token_sha256 FROM order_access",
    "SELECT * FROM money_operations",
    "SELECT order_status_view(1, decode(repeat('00', 32), 'hex'))",
    "SELECT payment_checkout_bind(1, decode(repeat('00', 32), 'hex'), 'x', NULL, TRUE)",
    "SELECT admin_resolve_payment(1, 'not_paid_confirmed', 'x')",
])
def test_worker_role_has_no_direct_write_access(shop_roles, as_role, sql):
    _order_via_app(shop_roles, as_role)
    shop_roles["worker"].refused(sql)


@pytest.mark.parametrize("sql", ["UPDATE orders SET status = 'paid' WHERE id = 1",
                                 "UPDATE money_operations SET amount_minor = 1",
                                 "SELECT apply_paid_order(1)"])
def test_admin_role_resolves_only_through_functions(shop_roles, as_role, sql):
    _order_via_app(shop_roles, as_role)
    shop_roles["admin"].refused(sql)


# ---------- позитив: весь процесс под своими ролями ----------

def test_full_flow_under_separate_roles(shop_roles, as_role, yookassa, monkeypatch):
    settings = PaymentSettings(enabled=True, shop_id=SHOP_ID, secret_key=SECRET, mode="test", api_url=yookassa.url,
                               return_url="https://shop.example/r", timeout_seconds=0.5, webhook_ip_check=False,
                               confirmation_hosts=("yoomoney.mock",))
    provider = YooKassaProvider.from_settings(settings, sleep=lambda s: None)
    web = PaymentService(provider, settings, connect=shop_roles["app"].connect)
    worker = PaymentService(provider, settings, connect=shop_roles["worker"].connect)

    as_role(shop_roles["app"])
    order, token = create_order_with_access([(1, 2)])                 # заказ v2 — роль приложения
    assert database.get_public_products_from_db()                      # каталог v1 читается
    web.checkout(order["id"], token)
    provider_id = next(iter(yookassa.payments))
    yookassa.set_status(provider_id, "succeeded")
    web.accept_notification(notification("payment.succeeded", provider_id), "x")

    # Веб-роль сама учесть оплату не может.
    with pytest.raises(Exception, match="permission denied"):
        web.sync_provider_payment(provider_id)

    assert [r[1] for r in worker.process_due_events()] == ["succeeded"]
    status = web.order_status(order["id"], token)
    assert (status["order"]["status"], status["payment_status"]) == ("paid", "succeeded")

    # Исполнение заказа — под ролью обработчика.
    from commerce import fulfillment
    as_role(shop_roles["worker"])
    fulfillment.process_pending_fulfillments()

    # Администратор видит деньги и может запросить возврат.
    rows = shop_roles["admin"].run("SELECT amount_minor FROM money_operations")
    assert rows == [(20000,)]
    payment_id = shop_roles["admin"].run("SELECT id FROM payments")[0][0]
    shop_roles["admin"].run("SELECT request_refund(%s, 20000, 'проверка прав')", (payment_id,))


def test_v1_order_still_works_under_app_role(shop_roles, as_role):
    as_role(shop_roles["app"])
    order = database.create_order_in_db(1)
    assert order["status"] == "pending"


def test_roles_sql_has_no_passwords_and_validates_names():
    text = db_roles.create_roles_sql("digital_store") + db_roles.grants_sql("public", "digital_store")
    assert "PASSWORD '" not in text.replace("PASSWORD '...'", "")
    assert "SUPERUSER" not in text.replace("NOSUPERUSER", "")
    assert "GRANT UPDATE ON public.orders" not in text and "products TO digital_store_app;" not in text.split(
        "GRANT SELECT")[0]
    for bad in ("public; DROP TABLE x", "Public", "a-b"):
        with pytest.raises(ValueError):
            db_roles.grants_sql(bad)
    assert "payment_record_provider_state" not in "\n".join(
        line for line in text.splitlines() if line.endswith("TO digital_store_app;"))


# ---------- rev2: веб-роль не может вмешаться в ЧУЖУЮ платёжную попытку ----------

def _attempt_for(shop_roles, as_role, yookassa_url=None):
    """Заказ и попытка оплаты под ролью приложения. -> (order_id, token_hex, payment_id)."""
    from payments.access import token_sha256_hex
    order, token = _order_via_app(shop_roles, as_role)
    token_hex = token_sha256_hex(token)
    rows = shop_roles["app"].run(
        "SELECT payment_id, error FROM payment_attempt_start(%s, decode(%s, 'hex'), 'yookassa', gen_random_uuid())",
        (order["id"], token_hex))
    assert rows[0][1] is None
    return order["id"], token_hex, rows[0][0]


def _payment_state(shop_roles, payment_id):
    owner = shop_roles["owner"]
    with owner.cursor() as cur:
        cur.execute("SELECT status, provider_payment_id, confirmation_url FROM payments WHERE id = %s", (payment_id,))
        row = cur.fetchone()
    owner.rollback()
    return row


def test_app_cannot_bind_or_mark_attempt_of_another_order(shop_roles, as_role):
    _, _, victim = _attempt_for(shop_roles, as_role)
    _, attacker_token, _ = _attempt_for(shop_roles, as_role)
    app = shop_roles["app"]

    assert app.run("SELECT payment_checkout_bind(%s, decode(%s, 'hex'), 'fake-pay', 'https://evil.example/', TRUE)",
                   (victim, attacker_token)) == [("not_found",)]
    assert app.run("SELECT payment_checkout_mark(%s, decode(%s, 'hex'), 'failed', 'x')",
                   (victim, attacker_token)) == [("not_found",)]
    assert app.run("SELECT payment_checkout_mark(%s, decode(repeat('00', 32), 'hex'), 'failed', 'x')",
                   (victim,)) == [("not_found",)]
    assert _payment_state(shop_roles, victim) == ("creating", None, None)


def test_app_with_the_right_secret_can_bind_its_own_attempt(shop_roles, as_role):
    _, token_hex, payment_id = _attempt_for(shop_roles, as_role)
    assert shop_roles["app"].run(
        "SELECT payment_checkout_bind(%s, decode(%s, 'hex'), 'pay-own-1', 'https://yoomoney.ru/x', TRUE)",
        (payment_id, token_hex)) == [("pending",)]
    assert _payment_state(shop_roles, payment_id) == ("pending", "pay-own-1", "https://yoomoney.ru/x")


def test_app_cannot_issue_secret_for_existing_order(shop_roles, as_role):
    order_id, _, _ = _attempt_for(shop_roles, as_role)
    v1 = database.create_order_in_db(1)            # заказ v1 без секрета
    for target in (order_id, v1["id"]):
        shop_roles["app"].refused("SELECT order_access_issue(%s, decode(repeat('cd', 32), 'hex'))", (target,),
                                  match="только в транзакции создания")


def test_status_view_requires_the_secret(shop_roles, as_role):
    order_id, token_hex, _ = _attempt_for(shop_roles, as_role)
    app = shop_roles["app"]
    assert app.run("SELECT order_status_view(%s, decode(repeat('00', 32), 'hex'))", (order_id,)) == [(None,)]
    view = app.run("SELECT order_status_view(%s, decode(%s, 'hex'))", (order_id, token_hex))[0][0]
    import json
    view = json.loads(view) if isinstance(view, str) else view
    assert view["order"]["id"] == order_id and view["payment"]["status"] == "creating"
    assert "token" not in json.dumps(view) and "idempotence" not in json.dumps(view)
