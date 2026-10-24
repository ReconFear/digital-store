"""Миграция 0004 (выдача цифровых ключей): 0000–0003 не изменены, переход 0003 -> 0004 на базе
с заказами и оплатами не меняет старые данные, повторный запуск, SECURITY DEFINER и search_path.

Каждый тест — во временной схеме тестовой базы (make_schema)."""

import hashlib
import re

import pytest

import migrate
import test_migrations
from test_migration_0003 import DATA_0002_SQL, DATA_0002_V2_ORDER, SECURITY_DEFINER as DEFINER_0003, _dir_until
from test_migrations import PROJECT_DIR, _query, _quiet, _run

make_schema = test_migrations.make_schema

# Контрольные суммы миграций, применённых у пользователя (PR1–PR3). Менять нельзя.
OLD_CHECKSUMS = {
    "0000": "76730405f960ab61b144f480a1cf531dd3642a3779d82d41e0892cd04d0cd411",
    "0001": "68cb7222461b808cdc69eaa7ebe716a48c4d2cf7c2cc7b8e2229e3959a49f81b",
    "0002": "4c83941c9b8daae22c99d1f31961446d64665dfa972070a348429d3b35d96231",
    "0003": "fa19742502c5241c77d47d2023032fc374424ef1d76532cc29c38c5b62f28366",
}

DEFINER_0004 = {"digital_keys_task_complete", "digital_keys_assign", "order_delivery_view",
                "encryption_key_check_values", "admin_import_digital_keys", "admin_withdraw_digital_key",
                "admin_resume_fulfillment", "admin_cancel_fulfillment", "admin_reconcile_digital_stock",
                "admin_register_encryption_key", "admin_digital_keys_keyring_sample", "admin_whoami"}
# Без EXECUTE у PUBLIC: все вызываемые функции 0004 (триггерные функции вызываются только триггерами).
NO_PUBLIC_EXECUTE_0004 = DEFINER_0004 | {
    "digital_key_stock_counts", "apply_paid_order", "shop_require_admin", "shop_is_owner_role",
    "digital_keys_import_core", "digital_keys_withdraw_core", "fulfillment_resume_core", "fulfillment_cancel_core",
    "digital_stock_reconcile_core", "encryption_key_register_core", "digital_keys_keyring_sample_core"}

SNAPSHOT_SQL = {
    "products": "SELECT * FROM products ORDER BY id",
    "orders": "SELECT * FROM orders ORDER BY id",
    "order_items": "SELECT * FROM order_items ORDER BY id",
    # Столбцы fulfillments, существовавшие до 0004 (0004 добавляет три новых, NULL у старых строк).
    "fulfillments": "SELECT id, order_item_id, fulfillment_type, status, attempts, last_error, next_attempt_at, "
                    "created_at, updated_at, fulfilled_at FROM fulfillments ORDER BY id",
    "payments": "SELECT * FROM payments ORDER BY id",
    "money_operations": "SELECT * FROM money_operations ORDER BY id",
    "refunds": "SELECT * FROM refunds ORDER BY id",
    "audit_log": "SELECT * FROM audit_log ORDER BY id",
    "order_access": "SELECT * FROM order_access ORDER BY order_id",
}


@pytest.fixture
def at_0003(make_schema, tmp_path):
    """Схема на 0003 с товарами, заказами, оплатой, возвратом и задачами (в том числе digital_keys)."""
    conn, schema = make_schema()
    applied = migrate.apply_migrations(conn, _dir_until(tmp_path, "0003"), log=_quiet)
    assert [m.version for m in applied] == ["0000", "0001", "0002", "0003"]
    _run(conn, DATA_0002_SQL)
    _run(conn, DATA_0002_V2_ORDER)
    # Старый товар digital_keys с остатком без ключей (как у магазина до PR5).
    _run(conn, "INSERT INTO products (name, price_minor, currency, quantity, is_for_sale, product_type, "
               "inventory_mode, fulfillment_type) VALUES ('Auto key', 50000, 'RUB', 7, TRUE, 'digital_key', "
               "'tracked', 'digital_keys')")
    _run(conn, "INSERT INTO orders (status, currency, total_minor) VALUES ('pending', 'RUB', 50000); "
               "INSERT INTO order_items (order_id, product_id, product_name, product_type, fulfillment_type, "
               "quantity, unit_price_minor, currency, line_total_minor) VALUES (currval('orders_id_seq'), 3, "
               "'Auto key', 'digital_key', 'digital_keys', 1, 50000, 'RUB', 50000)")
    order_id = _query(conn, "SELECT currval('orders_id_seq')")[0][0]
    _run(conn, f"INSERT INTO payments (order_id, provider, idempotence_key, amount_minor, currency) "
               f"VALUES ({order_id}, 'yookassa', gen_random_uuid(), 50000, 'RUB')")
    _run(conn, "UPDATE payments SET provider_payment_id = 'p-old', status = 'pending'")
    _run(conn, "SELECT payment_record_provider_state((SELECT id FROM payments WHERE provider_payment_id = 'p-old'), "
               "'p-old', 'succeeded', 50000, 'RUB', TRUE, TRUE, NULL)")
    _run(conn, "SELECT request_refund((SELECT id FROM payments WHERE provider_payment_id = 'p-old'), 100, 'тест')")
    return conn, schema, order_id


def test_old_migration_checksums_are_unchanged():
    found = {m.version: m.checksum for m in migrate.discover() if m.version in OLD_CHECKSUMS}
    assert found == OLD_CHECKSUMS


def test_0004_is_fifth_and_checksummed():
    # PR6: после 0004 идёт 0005 (проверка «последняя миграция» — в test_migration_0005.py).
    versions = [m.version for m in migrate.discover()]
    assert versions[:5] == ["0000", "0001", "0002", "0003", "0004"]
    m = migrate.discover()[4]
    assert m.path.name == "0004_digital_key_fulfillment.sql"
    assert m.checksum == hashlib.sha256(migrate._normalize(m.path.read_bytes()).encode("utf-8")).hexdigest()


def test_upgrade_from_0003_applies_only_0004_and_keeps_data(at_0003, tmp_path):
    conn, _, order_id = at_0003
    assert _query(conn, f"SELECT status FROM orders WHERE id = {order_id}") == [("paid",)]
    before = {name: _query(conn, sql) for name, sql in SNAPSHOT_SQL.items()}
    journal_before = _query(conn, "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version")

    # PR6: переход 0003 -> 0004 — на наборе миграций до 0004 (0004 -> 0005 — в test_migration_0005.py).
    until_0004 = _dir_until(tmp_path, "0004")
    applied = migrate.apply_migrations(conn, until_0004, log=_quiet)

    assert [m.version for m in applied] == ["0004"]
    assert {name: _query(conn, sql) for name, sql in SNAPSHOT_SQL.items()} == before
    journal = _query(conn, "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version")
    assert journal[:4] == journal_before
    assert _query(conn, "SELECT count(*) FROM fulfillments WHERE status_note IS NOT NULL OR cancel_reason IS NOT NULL "
                        "OR refund_hold_released_at IS NOT NULL") == [(0,)]
    for table in ("digital_keys", "digital_key_batches", "encryption_key_registry", "shop_security_settings"):
        assert _query(conn, f"SELECT count(*) FROM {table}") == [(0,)]
    # Повторный запуск ничего не делает.
    assert migrate.apply_migrations(conn, until_0004, log=_quiet) == []


def test_after_upgrade_legacy_data_is_safe(at_0003, tmp_path):
    """После 0004: старый товар с Q без ключей не продаётся (S = 0), его оплаченная до PR5 задача
    видна как ожидающая (W), выдача остановлена запросом возврата, Q не меняется миграцией."""
    conn, _, order_id = at_0003
    migrate.apply_migrations(conn, _dir_until(tmp_path, "0004"), log=_quiet)
    assert _query(conn, "SELECT quantity FROM products WHERE id = 3") == [(6,)]
    assert _query(conn, "SELECT sellable_quantity FROM product_sellable_stock WHERE product_id = 3") == [(0,)]
    assert _query(conn, "SELECT quantity, available, awaiting, surplus FROM digital_key_stock WHERE product_id = 3") \
        == [(6, 0, 1, -7)]
    fid = _query(conn, f"SELECT f.id FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id "
                       f"WHERE i.order_id = {order_id}")[0][0]
    assert _query(conn, f"SELECT digital_keys_assign({fid})->>'result'") == [("refund_hold",)]
    conn.rollback()
    # Ручное повышение остатка запрещено, понижение — можно; сверка — только вниз.
    with pytest.raises(Exception, match="больше обеспеченного"):
        _run(conn, "UPDATE products SET quantity = 8 WHERE id = 3")
    conn.rollback()
    assert _query(conn, "SELECT digital_stock_reconcile_core(3, 'pytest')->>'new_quantity'") == [("0",)]


def test_security_definer_search_path_and_public_execute(at_0003, tmp_path):
    conn, schema, _ = at_0003
    # PR6: состав функций до 0004 включительно; функции 0005 — в test_migration_0005.py.
    migrate.apply_migrations(conn, _dir_until(tmp_path, "0004"), log=_quiet)
    rows = _query(conn, """
        SELECT p.proname, p.prosecdef, coalesce(array_to_string(p.proacl, ','), ''),
               array_to_string(p.proconfig, ';')
        FROM pg_proc p WHERE p.pronamespace = %s::regnamespace""", (schema,))
    secdef = {name for name, definer, _, _ in rows if definer}
    assert secdef == DEFINER_0003 | DEFINER_0004
    for name, _, acl, config in rows:
        # В том числе apply_paid_order после CREATE OR REPLACE (сбрасывает proconfig) — путь закреплён заново.
        assert config == f"search_path=pg_catalog, {schema}, pg_temp", name
        if name in NO_PUBLIC_EXECUTE_0004 | DEFINER_0003:
            assert acl and not any(entry.startswith("=") for entry in acl.split(",")), (name, acl)


def test_apply_paid_order_change_is_minimal():
    """Изменение функции PR3 в 0004 — только проверка запаса ключей (сравнение с текстом 0003)."""
    import difflib
    old = (PROJECT_DIR / "migrations" / "0003_payments_security.sql").read_text(encoding="utf-8")
    new = (PROJECT_DIR / "migrations" / "0004_digital_key_fulfillment.sql").read_text(encoding="utf-8")

    def body(text, start):
        i = text.index(start)
        return text[i:text.index("$$;", i)].splitlines()

    diff = [line for line in difflib.unified_diff(body(old, "CREATE FUNCTION apply_paid_order"),
                                                  body(new, "CREATE OR REPLACE FUNCTION apply_paid_order"),
                                                  lineterm="", n=0)
            if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    removed = [line for line in diff if line.startswith("-")]
    assert removed == [
        "-CREATE FUNCTION apply_paid_order(p_order_id INTEGER) RETURNS JSONB",
        "-        SELECT p.id, p.quantity, p.is_for_sale, p.inventory_mode, n.needed",
        "-            SELECT product_id, sum(quantity)::BIGINT AS needed",
    ]
    assert len(diff) - len(removed) == 11   # 1 заголовок + 2 в выборке + 8 строк проверки


def test_schema_sql_contains_0004():
    # PR6: после текста 0004 в schema.sql идёт 0005 — проверяется вхождение, а не окончание файла.
    text = (PROJECT_DIR / "schema.sql").read_text(encoding="utf-8-sig")
    assert "CREATE TABLE digital_keys" in text and "digital_keys_assign" in text
    assert (PROJECT_DIR / "migrations" / "0004_digital_key_fulfillment.sql").read_text(encoding="utf-8-sig").strip() \
        in text


def test_temp_table_shadowing_does_not_affect_delivery_view(at_0003, tmp_path):
    """SECURITY DEFINER с закреплённым путём: временная таблица атакующего не подменяет order_access."""
    import database
    conn, schema, order_id = at_0003
    migrate.apply_migrations(conn, _dir_until(tmp_path, "0004"), log=_quiet)
    fake = hashlib.sha256(b"fake").hexdigest()
    attacker = database.get_connection()
    try:
        with attacker.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("CREATE TEMP TABLE order_access (order_id INTEGER, token_sha256 BYTEA)")
            cur.execute("INSERT INTO pg_temp.order_access VALUES (%s, decode(%s, 'hex'))", (order_id, fake))
            cur.execute("SELECT order_delivery_view(%s, decode(%s, 'hex'))", (order_id, fake))
            assert cur.fetchone()[0] is None
        attacker.rollback()
    finally:
        attacker.close()


def _declared_objects(versions):
    """(таблицы и представления, функции), объявленные в тексте миграций с этими номерами."""
    relations, functions = set(), set()
    for path in sorted((PROJECT_DIR / "migrations").glob("*.sql")):
        if path.name[:4] in versions:
            text = path.read_text(encoding="utf-8-sig")
            relations |= set(re.findall(r"^\s*CREATE (?:TABLE|VIEW)(?: IF NOT EXISTS)? ([a-z_]+)", text, re.MULTILINE))
            functions |= set(re.findall(r"^CREATE (?:OR REPLACE )?FUNCTION ([a-z_]+)\(", text, re.MULTILINE))
    return relations, functions


def test_ownership_step_lists_every_shop_object(make_schema, tmp_path):
    """deploy/db_roles.py --part ownership передаёт мигратору явный список объектов: схема после 0004 должна
    содержать ровно объекты этого списка, кроме созданных более поздними миграциями (иначе SECURITY
    DEFINER-функция осталась бы у прежнего владельца, например у суперпользователя). Объекты более поздних
    миграций берутся из их текста; та же проверка для всех миграций —
    test_migration_0005.py::test_ownership_step_lists_every_shop_object_after_0005."""
    from deploy import db_roles
    versions = [m.version for m in migrate.discover()]
    earlier = _declared_objects([v for v in versions if v <= "0004"])
    later = _declared_objects([v for v in versions if v > "0004"])
    # Объект, переопределённый позже (CREATE OR REPLACE), остаётся объектом 0000–0004.
    later_relations, later_functions = later[0] - earlier[0], later[1] - earlier[1]
    # Вычитается только то, что действительно есть в списках db_roles.py (иначе тест маскировал бы пропуск).
    assert later_relations <= set(db_roles.ALL_TABLES) and later_functions <= set(db_roles.SHOP_FUNCTIONS)

    conn, schema = make_schema()
    migrate.apply_migrations(conn, _dir_until(tmp_path, "0004"), log=_quiet)
    relations = {name for (name,) in _query(conn, "SELECT relname FROM pg_class WHERE relnamespace = %s::regnamespace "
                                                  "AND relkind IN ('r', 'p', 'v')", (schema,))}
    functions = {name for (name,) in _query(conn, "SELECT proname FROM pg_proc WHERE pronamespace = %s::regnamespace",
                                            (schema,))}
    assert relations == set(db_roles.ALL_TABLES) - later_relations
    assert functions == set(db_roles.SHOP_FUNCTIONS) - later_functions
