"""Миграция 0003 (платежи и безопасность): перенос 0002 -> 0003, повторный запуск,
неизменность старых миграций, защита финансовой истории, SECURITY DEFINER.

Каждый тест — во временной схеме тестовой базы (make_schema из test_migrations.py).
"""

import hashlib
import shutil

import pytest

import database
import migrate
import test_migrations
from test_migrations import PROJECT_DIR, _query, _quiet, _run

make_schema = test_migrations.make_schema

# Контрольные суммы миграций, уже установленных у пользователя (PR 1 / PR 2 rev3-fix).
# Если тест упал — кто-то изменил применённую миграцию: так делать нельзя, нужна новая.
OLD_CHECKSUMS = {
    "0000": "76730405f960ab61b144f480a1cf531dd3642a3779d82d41e0892cd04d0cd411",
    "0001": "68cb7222461b808cdc69eaa7ebe716a48c4d2cf7c2cc7b8e2229e3959a49f81b",
    "0002": "4c83941c9b8daae22c99d1f31961446d64665dfa972070a348429d3b35d96231",
}

SECURITY_DEFINER = {"order_access_issue", "order_status_view", "payment_attempt_start", "payment_attempt_bind",
                    "payment_attempt_mark", "payment_checkout_bind", "payment_checkout_mark", "payment_attempt_touch",
                    "provider_event_accept", "payment_record_provider_state", "admin_resolve_payment",
                    "admin_retry_paid_order", "request_refund"}
NO_PUBLIC_EXECUTE = SECURITY_DEFINER | {"apply_paid_order"}

DATA_0002_SQL = """
INSERT INTO products (name, price_minor, currency, quantity, is_for_sale, product_type, inventory_mode,
                      fulfillment_type)
VALUES ('Key', 99900, 'RUB', 5, TRUE, 'digital_key', 'tracked', 'manual'),
       ('Service', 150050, 'RUB', 0, TRUE, 'service', 'unlimited', 'manual');
INSERT INTO orders (product_id, price) VALUES (1, 999);
"""
DATA_0002_V2_ORDER = """
INSERT INTO orders (status, currency, total_minor) VALUES ('pending', 'RUB', 399850);
INSERT INTO order_items (order_id, product_id, product_name, product_type, fulfillment_type, quantity,
                         unit_price_minor, currency, line_total_minor)
VALUES (currval('orders_id_seq'), 1, 'Key', 'digital_key', 'manual', 1, 99900, 'RUB', 99900),
       (currval('orders_id_seq'), 2, 'Service', 'service', 'manual', 2, 149975, 'RUB', 299950);
"""
SNAPSHOT_SQL = {
    "products": "SELECT * FROM products ORDER BY id",
    "orders": "SELECT * FROM orders ORDER BY id",
    "order_items": "SELECT * FROM order_items ORDER BY id",
    "fulfillments": "SELECT * FROM fulfillments ORDER BY id",
}


def _dir_until(tmp_path, last):
    target = tmp_path / f"until_{last}"
    target.mkdir()
    for path in sorted((PROJECT_DIR / "migrations").glob("*.sql")):
        if path.name[:4] <= last:
            shutil.copy(path, target / path.name)
    return target


@pytest.fixture
def at_0002(make_schema, tmp_path):
    conn, schema = make_schema()
    applied = migrate.apply_migrations(conn, _dir_until(tmp_path, "0002"), log=_quiet)
    assert [m.version for m in applied] == ["0000", "0001", "0002"]
    _run(conn, DATA_0002_SQL)
    _run(conn, DATA_0002_V2_ORDER)
    _run(conn, "UPDATE orders SET status = 'paid', paid_at = now() WHERE id = 1")
    _run(conn, "INSERT INTO fulfillments (order_item_id, fulfillment_type) "
               "SELECT id, fulfillment_type FROM order_items WHERE order_id = 1")
    return conn, schema


def _snapshot(conn):
    return {name: _query(conn, sql) for name, sql in SNAPSHOT_SQL.items()}


def _commit_query(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.commit()
    return rows


def _fails(conn, sql, match, params=None):
    with pytest.raises(Exception, match=match):
        with conn.cursor() as cur:
            cur.execute(sql, params)
    conn.rollback()


# ---------- 18. старые миграции не изменены ----------

def test_old_migration_checksums_are_unchanged():
    found = {m.version: m.checksum for m in migrate.discover() if m.version in OLD_CHECKSUMS}
    assert found == OLD_CHECKSUMS


def test_new_migration_is_last_and_checksummed():
    # PR5: после 0003 идёт 0004 (проверка «последняя миграция» — в test_migration_0004.py).
    versions = [m.version for m in migrate.discover()]
    assert versions[:4] == ["0000", "0001", "0002", "0003"]
    m = migrate.discover()[3]
    assert m.checksum == hashlib.sha256(migrate._normalize(m.path.read_bytes()).encode("utf-8")).hexdigest()


# ---------- 15. 0002 -> 0003 с сохранением данных ----------

def test_upgrade_from_0002_applies_only_0003_and_keeps_products_and_orders(at_0002, tmp_path):
    conn, _ = at_0002
    before = _snapshot(conn)
    journal_before = _query(conn, "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version")

    # PR5: переход 0002 -> 0003 проверяется на наборе миграций до 0003 (0004 добавляет столбцы
    # в fulfillments; переход 0003 -> 0004 — в test_migration_0004.py).
    applied = migrate.apply_migrations(conn, _dir_until(tmp_path, "0003"), log=_quiet)

    assert [m.version for m in applied] == ["0003"]
    assert _snapshot(conn) == before
    journal = _query(conn, "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version")
    assert journal[:3] == journal_before
    assert {v: c for v, c, _ in journal[:3]} == OLD_CHECKSUMS
    for table in ("payments", "money_operations", "provider_events", "refunds", "audit_log", "order_access"):
        assert _query(conn, f"SELECT count(*) FROM {table}") == [(0,)]


def test_after_upgrade_old_rules_still_hold(at_0002):
    conn, _ = at_0002
    migrate.apply_migrations(conn, log=_quiet)
    _fails(conn, "UPDATE orders SET status = 'pending' WHERE id = 1", "переход")
    _fails(conn, "DELETE FROM order_items WHERE order_id = 2", "не удаляются")
    _run(conn, "INSERT INTO orders (product_id, price) VALUES (1, 999)")   # v1 по-прежнему работает
    assert _commit_query(conn, "SELECT apply_paid_order(3)->>'status'") == [("paid",)]
    assert _query(conn, "SELECT quantity FROM products WHERE id = 1") == [(4,)]


def test_reapplying_migrations_changes_nothing(at_0002):
    conn, schema = at_0002
    migrate.apply_migrations(conn, log=_quiet)
    structure = test_migrations._structure(conn, schema)
    data = _snapshot(conn)

    assert migrate.apply_migrations(conn, log=_quiet) == []
    assert test_migrations._structure(conn, schema) == structure
    assert _snapshot(conn) == data
    assert migrate.status(conn)["pending"] == []


def test_apply_paid_order_matches_python_behaviour_of_pr2(at_0002):
    conn, _ = at_0002
    migrate.apply_migrations(conn, log=_quiet)
    _run(conn, "UPDATE products SET quantity = 0 WHERE id = 1")
    result = _query(conn, "SELECT apply_paid_order(2)")[0][0]
    import json
    result = json.loads(result) if isinstance(result, str) else result
    assert result == {"error": "out_of_stock", "order_id": 2, "status": "requires_action",
                      "problems": [{"product_id": 1, "reason": "insufficient_stock"}]}
    for order_id, expected in ((1, {"status": "already_paid"}), (999, {"error": "order_not_found"})):
        value = _query(conn, "SELECT apply_paid_order(%s)", (order_id,))[0][0]
        assert (json.loads(value) if isinstance(value, str) else value) == expected


# ---------- безопасность функций ----------

def test_security_definer_functions_and_public_execute(at_0002, tmp_path):
    conn, schema = at_0002
    # PR5: состав функций 0003; функции 0004 — в test_migration_0004.py.
    migrate.apply_migrations(conn, _dir_until(tmp_path, "0003"), log=_quiet)
    rows = _query(conn, """
        SELECT p.proname, p.prosecdef, coalesce(array_to_string(p.proacl, ','), ''),
               array_to_string(p.proconfig, ';')
        FROM pg_proc p WHERE p.pronamespace = %s::regnamespace""", (schema,))
    secdef = {name for name, definer, _, _ in rows if definer}
    assert secdef == SECURITY_DEFINER
    for name, _, acl, config in rows:
        assert config == f"search_path=pg_catalog, {schema}, pg_temp", name
        if name in NO_PUBLIC_EXECUTE:
            # proacl задан (не NULL = «по умолчанию PUBLIC EXECUTE») и не содержит записи для PUBLIC ("=X/").
            assert acl and not any(entry.startswith("=") for entry in acl.split(",")), (name, acl)


def test_security_definer_ignores_temp_table_shadowing(at_0002):
    conn, schema = at_0002
    migrate.apply_migrations(conn, log=_quiet)
    token = hashlib.sha256(b"real").hexdigest()
    _run(conn, f"INSERT INTO order_access (order_id, token_sha256) VALUES (2, decode('{token}', 'hex'))")

    attacker = database.get_connection()   # отдельная сессия, как у атакующего
    try:
        with attacker.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("CREATE TEMP TABLE order_access (order_id INTEGER, token_sha256 BYTEA)")
            cur.execute("INSERT INTO pg_temp.order_access VALUES (2, decode(%s, 'hex'))",
                        (hashlib.sha256(b"fake").hexdigest(),))
            cur.execute("SELECT error FROM payment_attempt_start(2, decode(%s, 'hex'), 'yookassa', gen_random_uuid())",
                        (hashlib.sha256(b"fake").hexdigest(),))
            assert cur.fetchone()[0] == "not_found"
        attacker.rollback()
    finally:
        attacker.close()


# ---------- защита финансовой истории ----------

@pytest.fixture
def paid(at_0002):
    conn, _ = at_0002
    migrate.apply_migrations(conn, log=_quiet)
    token = hashlib.sha256(b"t").hexdigest()
    _run(conn, f"INSERT INTO order_access (order_id, token_sha256) VALUES (2, decode('{token}', 'hex'))")
    pid = _commit_query(conn, f"SELECT payment_id FROM payment_attempt_start(2, decode('{token}', 'hex'), "
                              "'yookassa', gen_random_uuid())")[0][0]
    _run(conn, f"SELECT payment_attempt_bind({pid}, 'pay-1', 'https://pay.example/1', TRUE)")
    _run(conn, f"SELECT payment_record_provider_state({pid}, 'pay-1', 'succeeded', 399850, 'RUB', TRUE, TRUE, NULL)")
    return conn, pid


def test_verified_success_records_money_and_pays_order_in_one_transaction(paid):
    conn, pid = paid
    assert _query(conn, "SELECT status FROM orders WHERE id = 2") == [("paid",)]
    assert _query(conn, "SELECT kind, amount_minor, currency FROM money_operations") == [("payment", 399850, "RUB")]
    assert _query(conn, "SELECT status FROM payments") == [("succeeded",)]
    assert _query(conn, "SELECT quantity FROM products WHERE id = 1") == [(4,)]


@pytest.mark.parametrize("sql", [
    "UPDATE money_operations SET amount_minor = 1",
    "DELETE FROM money_operations",
    "UPDATE audit_log SET action = 'x'",
    "DELETE FROM audit_log",
    "DELETE FROM payments",
    "UPDATE payments SET status = 'canceled'",
    "UPDATE payments SET amount_minor = 1",
    "UPDATE order_access SET token_sha256 = decode(repeat('00', 32), 'hex')",
    "DELETE FROM order_access",
])
def test_financial_history_cannot_be_rewritten(paid, sql):
    conn, _ = paid
    _fails(conn, sql, ".")
    assert _query(conn, "SELECT count(*) FROM money_operations") == [(1,)]


@pytest.mark.parametrize("table", ["payments", "money_operations", "refunds", "audit_log", "order_access"])
def test_truncate_of_financial_tables_is_refused(paid, table):
    conn, _ = paid
    _fails(conn, f"TRUNCATE {table} CASCADE", "TRUNCATE")
    _fails(conn, "SET LOCAL digital_store.allow_history_delete = 'on'; " + f"TRUNCATE {table} CASCADE", "TRUNCATE")


def test_paid_order_gets_no_new_payment_attempt(paid):
    conn, _ = paid
    token = hashlib.sha256(b"t").hexdigest()
    assert _query(conn, f"SELECT error FROM payment_attempt_start(2, decode('{token}', 'hex'), 'yookassa', "
                        "gen_random_uuid())") == [("not_payable",)]


def test_payment_attempt_only_for_pending_orders_with_exact_total(at_0002):
    conn, _ = at_0002
    migrate.apply_migrations(conn, log=_quiet)
    _fails(conn, "INSERT INTO payments (order_id, provider, idempotence_key, amount_minor, currency) "
                 "VALUES (1, 'yookassa', gen_random_uuid(), 99900, 'RUB')", "нельзя оплачивать")   # уже paid
    _fails(conn, "INSERT INTO payments (order_id, provider, idempotence_key, amount_minor, currency) "
                 "VALUES (2, 'yookassa', gen_random_uuid(), 399849, 'RUB')", "равна итогу")
    _fails(conn, "INSERT INTO payments (order_id, provider, idempotence_key, amount_minor, currency, status) "
                 "VALUES (2, 'yookassa', gen_random_uuid(), 399850, 'RUB', 'succeeded')", "creating")


def test_one_active_attempt_per_order(at_0002):
    conn, _ = at_0002
    migrate.apply_migrations(conn, log=_quiet)
    _run(conn, "INSERT INTO payments (order_id, provider, idempotence_key, amount_minor, currency) "
               "VALUES (2, 'yookassa', gen_random_uuid(), 399850, 'RUB')")
    _fails(conn, "INSERT INTO payments (order_id, provider, idempotence_key, amount_minor, currency) "
                 "VALUES (2, 'yookassa', gen_random_uuid(), 399850, 'RUB')", "payments_one_active_per_order")


def test_succeeded_requires_money_operation_even_for_table_owner(at_0002):
    conn, _ = at_0002
    migrate.apply_migrations(conn, log=_quiet)
    _run(conn, "INSERT INTO payments (order_id, provider, idempotence_key, amount_minor, currency) "
               "VALUES (2, 'yookassa', gen_random_uuid(), 399850, 'RUB')")
    _run(conn, "UPDATE payments SET provider_payment_id = 'p', status = 'pending'")
    _fails(conn, "UPDATE payments SET status = 'succeeded'", "без учтённой денежной операции")


def test_schema_sql_contains_0003(make_schema):
    text = (PROJECT_DIR / "schema.sql").read_text(encoding="utf-8-sig")
    assert "CREATE TABLE payments" in text and "payment_record_provider_state" in text
