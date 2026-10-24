"""PR6: права PostgreSQL на аккаунты покупателей (миграция 0005) под настоящими ролями.

REV4 §7 T-A1…T-A7, REV5 §8 D2, K10a через административную обёртку. Как test_db_privileges.py, СОЗДАЁТ
временные роли (dst_<случайно>_*) и удаляет их в конце: только при DS_TEST_CREATE_ROLES=1 и праве CREATEROLE
у тестовой роли. Временная схема — со всеми миграциями 0000–0005 и правами deploy/db_roles.py (grants_sql).
"""

import base64
import hashlib
import json
import os

import psycopg
import pytest

from deploy import db_roles
from test_db_privileges import current_database, make_schema, shop_roles  # noqa: F401  (фикстуры)
from test_migration_0005 import (
    CHANGE, EMAIL, LOGIN, REGISTER, _Background, _link, _new_order, _proof, _token, _wait_blocked,
)
from test_migrations import _query, _run

ZERO = "decode(repeat('00', 32), 'hex')"
PRODUCT = 1   # товар 'A' (10000, остаток 5) создаёт фикстура shop_roles

# Внутренние функции 0005: SECURITY INVOKER, EXECUTE не выдаётся никому.
INTERNAL_CALLS = [
    f"SELECT customer_session_customer({ZERO})",
    "SELECT auth_session_refusal()",
    "SELECT customer_sessions_invalidate(1)",
    "SELECT auth_require_read_committed()",
    "SELECT auth_check_email('a@example.test')",
    "SELECT customer_lookup_core('a@example.test', 'x')",
    "SELECT customer_disable_core(1, 'x', 'x')",
    "SELECT customer_revoke_sessions_core(1, 'x', 'x')",
    "SELECT customer_release_email_core(1, 'x', 'x')",
    "SELECT auth_settings_update_core(TRUE, TRUE, 'x')",
]
ADMIN_CALLS = [
    "SELECT admin_customer_lookup('a@example.test')",
    "SELECT admin_customer_disable(1, 'x')",
    "SELECT admin_customer_revoke_sessions(1, 'x')",
    "SELECT admin_customer_release_email(1, 'x')",
    "SELECT admin_auth_settings_update(TRUE, TRUE)",
]
APP_CALLS = [
    f"SELECT auth_session_resolve({ZERO})",
    f"SELECT auth_register('a@example.test', repeat('A', 43), 1::smallint, {ZERO})",
    f"SELECT auth_login('a@example.test', repeat('A', 43), 1::smallint, NULL, NULL, {ZERO}, NULL)",
    f"SELECT customer_order_link_new({ZERO}, 1)",
    f"SELECT customer_orders_view({ZERO})",
]
WORKER_CALLS = ["SELECT auth_maintenance_purge()"]
PGCRYPTO_CALLS = [
    "SELECT ext_pgcrypto.crypt('a', 'b')",
    "SELECT ext_pgcrypto.gen_salt('bf')",
    "SELECT ext_pgcrypto.gen_random_bytes(8)",
]


@pytest.fixture
def shop(shop_roles):
    """Роли магазина; аккаунты включены, bcrypt cost 4 (только во временной тестовой схеме)."""
    _run(shop_roles["owner"], "UPDATE auth_settings SET bcrypt_cost = 4, accounts_enabled = TRUE, "
                              "registration_open = TRUE")
    return shop_roles


def _one(role, sql, params=None):
    return role.run(sql, params)[0][0]


def _owner(shop, sql, params=None):
    return _query(shop["owner"], sql, params)


def _register(shop, email=EMAIL):
    proof, session = _proof(), _token()
    assert _one(shop["app"], REGISTER, (email, proof, 1, session)) == {"result": "created"}
    return proof, session


def _order_in_app_transaction(shop, session, token=None):
    """Заказ в транзакции роли app, как в PR3: состав, гостевой секрет, затем привязка. -> (id, ответ)."""
    conn = shop["app"].connect()
    try:
        with conn.cursor() as cur:
            order_id = _new_order(cur, PRODUCT, token_sha256=token)
            result = _link(cur, session, order_id)
        conn.commit()
        return order_id, result
    finally:
        conn.rollback()
        conn.close()


# ---------- T-A1: таблицы аккаунтов и точный перечень функций ----------

@pytest.mark.parametrize("table", db_roles.ACCOUNT_TABLES)
@pytest.mark.parametrize("kind", ["app", "worker", "admin"])
def test_account_tables_are_closed_to_shop_roles(shop, kind, table):
    shop[kind].refused(f"SELECT count(*) FROM {table}")


@pytest.mark.parametrize("sql", [
    "SELECT password_hash FROM customers",
    "SELECT token_sha256 FROM customer_sessions",
    "SELECT dummy_hash FROM auth_settings",
    f"INSERT INTO customer_sessions (customer_id, token_sha256, auth_epoch, idle_expires_at, expires_at) "
    f"VALUES (1, {ZERO}, 1, now(), now())",
    "INSERT INTO customer_orders (order_id, customer_id) VALUES (1, 1)",
    "UPDATE customers SET auth_epoch = 1",
    "UPDATE customer_sessions SET revoked_at = NULL",
    "UPDATE auth_settings SET accounts_enabled = TRUE",
    "UPDATE auth_throttle SET attempts = 0",
    "DELETE FROM auth_throttle",
    "DELETE FROM customer_orders",
    "TRUNCATE customers CASCADE",
])
def test_app_cannot_read_or_write_account_tables(shop, sql):
    shop["app"].refused(sql)


@pytest.mark.parametrize("sql", INTERNAL_CALLS + ADMIN_CALLS + WORKER_CALLS + PGCRYPTO_CALLS)
def test_app_has_no_internal_admin_worker_or_pgcrypto_functions(shop, sql):
    shop["app"].refused(sql)


@pytest.mark.parametrize("sql", INTERNAL_CALLS + ADMIN_CALLS + APP_CALLS + PGCRYPTO_CALLS)
def test_worker_has_only_purge(shop, sql):
    shop["worker"].refused(sql)


@pytest.mark.parametrize("sql", INTERNAL_CALLS + APP_CALLS + WORKER_CALLS + PGCRYPTO_CALLS)
def test_admin_has_only_admin_functions(shop, sql):
    shop["admin"].refused(sql)


def test_execute_matrix_is_exact(shop):
    """Вызываемые (нетриггерные) функции схемы для каждой роли — ровно матрица deploy/db_roles.py."""
    for kind, functions in (("app", db_roles.APP_FUNCTIONS), ("worker", db_roles.WORKER_FUNCTIONS),
                            ("admin", db_roles.ADMIN_FUNCTIONS)):
        rows = _owner(shop, """
            SELECT p.proname::text FROM pg_proc p
            WHERE p.pronamespace = %s::regnamespace AND p.prorettype <> 'trigger'::regtype
              AND has_function_privilege(%s, p.oid, 'EXECUTE')""", (shop["schema"], shop[kind].name))
        assert {name for (name,) in rows} == {f.split("(", 1)[0] for f in functions}, kind


def test_worker_purges(shop):
    assert set(_one(shop["worker"], "SELECT auth_maintenance_purge()")) == {"throttle_deleted", "sessions_deleted"}


# ---------- T-A6 и D2: ext_pgcrypto, создание объектов, pg_temp ----------

def test_pgcrypto_schema_is_closed_to_shop_roles_and_public(shop):
    group = shop["app"].name.rsplit("_", 1)[0] + "_admin"
    for role in (shop["app"].name, shop["worker"].name, shop["admin"].name, group, "public"):
        assert _owner(shop, "SELECT has_schema_privilege(%s, 'ext_pgcrypto', 'USAGE') "
                            "OR has_schema_privilege(%s, 'ext_pgcrypto', 'CREATE')", (role, role)) == [(False,)], role


@pytest.mark.parametrize("kind", ["app", "worker", "admin"])
@pytest.mark.parametrize("sql", [
    "CREATE FUNCTION customer_session_customer(BYTEA, INTEGER) RETURNS BIGINT LANGUAGE sql AS 'SELECT 1::BIGINT'",
    "CREATE TABLE auth_settings_shadow (id INT)",
    "CREATE VIEW customers_shadow AS SELECT 1 AS id",
    "CREATE SCHEMA pr6_shadow",
    "CREATE TABLE ext_pgcrypto.shadow (id INT)",
    "CREATE FUNCTION ext_pgcrypto.crypt_shadow() RETURNS INT LANGUAGE sql AS 'SELECT 1'",
])
def test_shop_roles_cannot_create_objects_in_resolvable_schemas(shop, kind, sql):
    shop[kind].refused(sql)


def test_temp_tables_follow_the_temporary_privilege(shop):
    """В CI у PUBLIC отозван TEMPORARY (database_sql) — временных объектов у ролей магазина нет вовсе.
    Где TEMPORARY есть, их безвредность проверяет следующий тест."""
    has_temp = _owner(shop, "SELECT has_database_privilege(%s, current_database(), 'TEMPORARY')",
                      (shop["app"].name,))[0][0]
    if has_temp:
        assert shop["app"].run("CREATE TEMP TABLE probe (id INT)") is None
    else:
        shop["app"].refused("CREATE TEMP TABLE probe (id INT)")


def test_temp_objects_of_app_cannot_shadow_account_functions(shop):
    """D2: даже с выданным TEMPORARY временные таблицы и функции app с именами объектов магазина не влияют
    на auth_login / auth_session_resolve (закреплённый search_path, pg_temp последним, pgcrypto с схемой)."""
    owner, app, schema = shop["owner"], shop["app"], shop["schema"]
    proof, _ = _register(shop)
    database = current_database(owner)
    _run(owner, f"GRANT TEMPORARY ON DATABASE {database} TO {app.name}")
    try:
        conn = app.connect()
        try:
            fake = _token()
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO pg_temp, {schema}")
                cur.execute("CREATE TEMP TABLE auth_settings (singleton BOOLEAN, accounts_enabled BOOLEAN)")
                cur.execute("INSERT INTO pg_temp.auth_settings VALUES (TRUE, FALSE)")
                cur.execute("CREATE TEMP TABLE customer_sessions (token_sha256 BYTEA, customer_id BIGINT)")
                cur.execute("INSERT INTO pg_temp.customer_sessions VALUES (%s, 1)", (fake,))
                cur.execute("CREATE FUNCTION pg_temp.crypt(TEXT, TEXT) RETURNS TEXT LANGUAGE sql AS 'SELECT $2'")
                cur.execute("CREATE FUNCTION pg_temp.customer_session_customer(BYTEA) RETURNS BIGINT "
                            "LANGUAGE sql AS 'SELECT 1::BIGINT'")
                login = f"SELECT {schema}.auth_login(%s, %s, 1::smallint, NULL, NULL, %s, NULL)"
                cur.execute(login, (EMAIL, proof, _token()))
                assert cur.fetchone()[0] == {"result": "ok"}           # настоящие настройки и хэш
                cur.execute(login, (EMAIL, _proof(), _token()))
                assert cur.fetchone()[0] == {"result": "invalid"}      # pg_temp.crypt не подменил проверку
                cur.execute(f"SELECT {schema}.auth_session_resolve(%s)", (fake,))
                assert cur.fetchone()[0] == {"valid": False}           # pg_temp.customer_sessions не читается
            conn.commit()
        finally:
            conn.close()
    finally:
        _run(owner, f"REVOKE TEMPORARY ON DATABASE {database} FROM {app.name}")


# ---------- T-A2…T-A5: путь покупателя под ролью app ----------

def test_full_account_flow_under_app_role_keeps_guest_access(shop):
    app = shop["app"]
    outputs = []

    def call(sql, params=None):
        value = _one(app, sql, params)
        outputs.append(value)
        return value

    proof, reg_session = _register(shop)
    session = _token()
    assert call(LOGIN, (EMAIL, proof, 1, None, None, session, reg_session)) == {"result": "ok"}
    assert call("SELECT auth_session_resolve(%s)", (session,)) == {"valid": True}
    assert call("SELECT auth_session_resolve(%s)", (reg_session,)) == {"valid": False}
    assert call("SELECT customer_profile_view(%s)", (session,))["email"] == EMAIL

    token = hashlib.sha256(os.urandom(32)).digest()
    order_id, linked = _order_in_app_transaction(shop, session, token)
    outputs.append(linked)
    assert linked == {"result": "linked"}
    assert [o["id"] for o in call("SELECT customer_orders_view(%s)", (session,))] == [order_id]
    assert call("SELECT customer_order_view(%s, %s)", (session, order_id))["order"]["id"] == order_id
    # Гостевой доступ PR3 к тому же заказу — по access_token, как и без аккаунта.
    assert _one(app, "SELECT order_status_view(%s, %s)", (order_id, token))["order"]["id"] == order_id
    assert _one(app, "SELECT order_status_view(%s, %s)", (order_id, bytes(32))) is None

    call("SELECT auth_session_touch(%s)", (session,))
    new_proof, new_session = _proof(), _token()
    assert call(CHANGE, (session, proof, 1, None, None, new_proof, 1, new_session)) == {"result": "ok"}
    assert call("SELECT auth_sessions_revoke_all(%s)", (new_session,)) == {"result": "ok"}
    assert call("SELECT auth_session_revoke(%s)", (new_session,)) == {"result": "ok"}

    # T-A2: ни один ответ не содержит хэшей паролей, доказательств и токенов.
    text = json.dumps(outputs, default=str)
    secrets_in_db = [h for (h,) in _owner(shop, "SELECT password_hash FROM customers")] + \
                    [h for (h,) in _owner(shop, "SELECT dummy_hash FROM auth_settings")]
    for value in secrets_in_db:
        assert value not in text and value[7:] not in text
    for raw in (reg_session, session, new_session, token):
        assert raw.hex() not in text and base64.b64encode(raw).decode() not in text
    assert proof not in text and new_proof not in text


def test_login_with_correct_email_and_arbitrary_proof_creates_nothing(shop):
    """T-A3: без перца доказательство не построить — произвольное доказательство даёт 'invalid'."""
    _register(shop)
    before = _owner(shop, "SELECT count(*) FROM customer_sessions")
    for _ in range(3):
        assert _one(shop["app"], LOGIN, (EMAIL, _proof(), 1, None, None, _token(), None)) == {"result": "invalid"}
    assert _owner(shop, "SELECT count(*) FROM customer_sessions") == before


def test_app_cannot_link_committed_or_other_transaction_orders(shop):
    """T-A4: чужой зафиксированный заказ, заказ другой транзакции и повторная привязка — отказ."""
    app = shop["app"]
    _, session = _register(shop)
    other = app.connect()
    try:
        with other.cursor() as cur:
            committed = _new_order(cur, PRODUCT)
        other.commit()
        with other.cursor() as cur:
            uncommitted = _new_order(cur, PRODUCT)              # другая транзакция, без COMMIT
        for order_id in (committed, uncommitted):
            app.refused("SELECT customer_order_link_new(%s, %s)", (session, order_id), match="только новый заказ")
    finally:
        other.rollback()
        other.close()
    order_id, linked = _order_in_app_transaction(shop, session)
    assert linked == {"result": "linked"}
    app.refused("SELECT customer_order_link_new(%s, %s)", (session, order_id), match="только новый заказ")
    assert _owner(shop, "SELECT count(*) FROM customer_orders") == [(1,)]


def test_foreign_and_missing_orders_look_the_same(shop):
    """T-A5: чужой заказ — тот же ответ, что несуществующий."""
    app = shop["app"]
    _, owner_session = _register(shop)
    _, other_session = _register(shop, email="other@example.test")
    order_id, _ = _order_in_app_transaction(shop, owner_session)
    foreign = _one(app, "SELECT customer_order_view(%s, %s)", (other_session, order_id))
    missing = _one(app, "SELECT customer_order_view(%s, %s)", (other_session, 10 ** 9))
    assert foreign is None and missing is None
    assert _one(app, "SELECT customer_orders_view(%s)", (other_session,)) == []


# ---------- T-A7: администрирование ----------

@pytest.mark.parametrize("sql", ADMIN_CALLS)
def test_admin_functions_refuse_the_schema_owner(shop, sql):
    owner = shop["owner"]
    with pytest.raises(psycopg.errors.RaiseException, match="суперпользовател|владельцем"):
        with owner.cursor() as cur:
            cur.execute(sql)
    owner.rollback()


def test_admin_functions_work_for_admin_and_audit_without_email(shop):
    app, admin = shop["app"], shop["admin"]
    proof, session = _register(shop)
    found = _one(admin, "SELECT admin_customer_lookup(%s)", (EMAIL,))
    assert found["result"] == "found" and set(found) == {"result", "customer_id", "status", "created_at"}
    cid = found["customer_id"]

    assert _one(admin, "SELECT admin_customer_revoke_sessions(%s, 'проверка')", (cid,)) == {"result": "ok"}
    assert _one(app, "SELECT auth_session_resolve(%s)", (session,)) == {"valid": False}
    assert _one(admin, "SELECT admin_customer_disable(%s, 'проверка')", (cid,)) == {"result": "ok"}
    assert _one(app, LOGIN, (EMAIL, proof, 1, None, None, _token(), None)) == {"result": "invalid"}
    assert _one(admin, "SELECT admin_customer_release_email(%s, 'проверка')", (cid,)) == {"result": "ok"}
    _register(shop)                                                    # адрес освобождён
    assert _one(admin, "SELECT admin_auth_settings_update(FALSE, FALSE)")["result"] == "ok"
    assert _one(app, LOGIN, (EMAIL, proof, 1, None, None, _token(), None)) == {"result": "disabled"}

    audit = _owner(shop, "SELECT action, details->>'by' FROM audit_log WHERE action LIKE 'admin.%' ORDER BY id")
    assert [action for action, _ in audit] == [
        "admin.customer_lookup", "admin.customer_revoke_sessions", "admin.customer_disable",
        "admin.customer_release_email", "admin.auth_settings_update"]
    assert {by for _, by in audit} == {admin.name}
    assert _owner(shop, "SELECT count(*) FROM audit_log WHERE details::text LIKE %s", (f"%{EMAIL}%",)) == [(0,)]


def test_k10a_release_by_admin_role_makes_waiting_link_refuse(shop):
    """K10a через настоящую обёртку admin_customer_release_email: привязка (роль app) ждёт FOR SHARE и после
    COMMIT освобождения получает session_invalid; заказ не создаётся."""
    owner, app, admin = shop["owner"], shop["app"], shop["admin"]
    _, session = _register(shop)
    cid = _owner(shop, "SELECT id FROM customers")[0][0]
    orders_before = _owner(shop, "SELECT count(*) FROM orders")
    admin_conn, buyer = admin.connect(), app.connect()
    try:
        with admin_conn.cursor() as cur:
            cur.execute("SELECT admin_customer_release_email(%s, 'K10a')", (cid,))
            assert cur.fetchone()[0] == {"result": "ok"}
        with buyer.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            buyer_pid = cur.fetchone()[0]
        buyer.commit()

        def order_transaction():
            with buyer.cursor() as cur:
                return _link(cur, session, _new_order(cur, PRODUCT))

        step = _Background(order_transaction)
        _wait_blocked(owner, buyer_pid)
        admin_conn.commit()
        step.join()
        assert step.error is None, step.error
        assert step.result == {"result": "session_invalid"}
        buyer.rollback()
    finally:
        for conn in (admin_conn, buyer):
            conn.rollback()
            conn.close()
    assert _owner(shop, "SELECT count(*) FROM orders") == orders_before
    assert _owner(shop, "SELECT count(*) FROM customer_orders") == [(0,)]
