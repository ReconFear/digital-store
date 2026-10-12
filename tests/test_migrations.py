"""Тесты системы миграций (migrate.py).

Интеграционные тесты работают в тестовой базе, но каждый — в собственной временной
схеме PostgreSQL (ds_mig_xxxx), которая удаляется после теста. Таблицы public
тестовой базы (products, orders из seed.sql) при этом не затрагиваются.
"""

import shutil
import uuid
from pathlib import Path

import pytest

import database
import migrate

PROJECT_DIR = Path(__file__).resolve().parent.parent

OLD_STRUCTURE_SQL = """
CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    price INTEGER NOT NULL CHECK (price >= 0),
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    is_for_sale BOOLEAN NOT NULL
);
CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    price INTEGER NOT NULL CHECK (price >= 0)
);
INSERT INTO products (name, price, quantity, is_for_sale)
VALUES ('Old product', 999, 14, TRUE), ('Hidden product', 1499, 0, FALSE);
INSERT INTO orders (product_id, price) VALUES (1, 999);
"""

# Колонки товаров, существовавшие до PR 2. Проверки «данные товаров не изменились»
# сравнивают именно их: миграция 0002 добавляет новые колонки, но не трогает старые.
LEGACY_PRODUCT_COLUMNS = "SELECT id, name, price, quantity, is_for_sale FROM products ORDER BY id"

# Тот же SQL, что в историческом migration_001_order_status.sql, без BEGIN/COMMIT.
MANUAL_001_SQL = """
ALTER TABLE orders
ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'legacy'
CHECK (status IN ('pending', 'paid', 'cancelled', 'legacy'));
ALTER TABLE orders
ALTER COLUMN status SET DEFAULT 'pending';
"""


def _quiet(*args, **kwargs):
    pass


@pytest.fixture
def make_schema():
    """Создаёт пустые временные схемы; соединение работает внутри последней созданной."""
    created = []
    connections = []

    def factory():
        conn = database.get_connection()
        schema = f"ds_mig_{uuid.uuid4().hex[:10]}"
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"SET search_path TO {schema}")
        conn.commit()
        created.append(schema)
        connections.append(conn)
        return conn, schema

    yield factory

    for conn in connections:
        conn.rollback()
        conn.close()
    with database.get_connection() as cleanup:
        with cleanup.cursor() as cur:
            for schema in created:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")


def _run(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _query(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.rollback()
    return rows


def _table_exists(conn, table):
    return _query(conn, "SELECT to_regclass(%s) IS NOT NULL", (table,))[0][0]


def _applied_versions(conn):
    return [row[0] for row in _query(conn, "SELECT version FROM schema_migrations ORDER BY version")]


def _structure(conn, schema):
    """Структура таблиц схемы без служебной schema_migrations и без имени схемы."""
    columns = _query(
        conn,
        """
        SELECT table_name, column_name, data_type, is_nullable, coalesce(column_default, '')
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name <> 'schema_migrations'
        ORDER BY table_name, ordinal_position
        """,
        (schema,),
    )
    constraints = _query(
        conn,
        """
        SELECT rel.relname, con.conname, pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_namespace ns ON ns.oid = rel.relnamespace
        WHERE ns.nspname = %s AND rel.relname <> 'schema_migrations'
        ORDER BY 1, 2
        """,
        (schema,),
    )
    indexes = _query(
        conn,
        """
        SELECT tablename, indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = %s AND tablename <> 'schema_migrations'
        ORDER BY 1, 2
        """,
        (schema,),
    )
    triggers = _query(
        conn,
        """
        SELECT rel.relname, tg.tgname, pg_get_triggerdef(tg.oid)
        FROM pg_trigger tg
        JOIN pg_class rel ON rel.oid = tg.tgrelid
        JOIN pg_namespace ns ON ns.oid = rel.relnamespace
        WHERE ns.nspname = %s AND NOT tg.tgisinternal
        ORDER BY 1, 2
        """,
        (schema,),
    )
    functions = _query(
        conn,
        """
        SELECT p.proname, pg_get_functiondef(p.oid)
        FROM pg_proc p
        JOIN pg_namespace ns ON ns.oid = p.pronamespace
        WHERE ns.nspname = %s
        ORDER BY 1
        """,
        (schema,),
    )
    text = repr((columns, constraints, indexes, triggers, functions))
    return text.replace(schema + ".", "").replace(schema, "<schema>")


# ---------- без базы: проверка файлов ----------

def test_project_migrations_are_valid():
    migrations = migrate.discover()

    assert [m.version for m in migrations][:2] == ["0000", "0001"]


def test_checksum_ignores_line_endings_and_bom(tmp_path):
    (tmp_path / "0000_a.sql").write_bytes(b"SELECT 1;\nSELECT 2;\n")
    lf = migrate.discover(tmp_path)[0].checksum

    (tmp_path / "0000_a.sql").write_bytes(b"\xef\xbb\xbfSELECT 1;\r\nSELECT 2;\r\n")
    crlf = migrate.discover(tmp_path)[0].checksum

    assert lf == crlf


@pytest.mark.parametrize("bad_name", ["1_init.sql", "0001-init.sql", "0001_Init.sql", "notes.txt"])
def test_bad_file_names_are_rejected(tmp_path, bad_name):
    (tmp_path / bad_name).write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(migrate.MigrationError, match="Неверное имя"):
        migrate.discover(tmp_path)


def test_duplicate_versions_are_rejected(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0001_b.sql").write_text("SELECT 1;", encoding="utf-8")

    with pytest.raises(migrate.MigrationError, match="Две миграции"):
        migrate.discover(tmp_path)


@pytest.mark.parametrize("statement", ["BEGIN;", "commit;", "  ROLLBACK ;", "START TRANSACTION;", "END;"])
def test_transaction_control_is_rejected(tmp_path, statement):
    (tmp_path / "0000_a.sql").write_text(f"{statement}\nSELECT 1;\n", encoding="utf-8")

    with pytest.raises(migrate.MigrationError, match="управление транзакцией"):
        migrate.discover(tmp_path)


def test_plpgsql_begin_end_is_allowed(tmp_path):
    (tmp_path / "0000_a.sql").write_text(
        "DO $$\nBEGIN\n    PERFORM 1;\nEND;\n$$;\n", encoding="utf-8"
    )

    assert len(migrate.discover(tmp_path)) == 1


def test_cli_refuses_to_apply_to_non_test_database_without_confirmation(capsys):
    # Отказ происходит до подключения к базе.
    code = migrate.main(["apply", "--database", "digital_store"])

    assert code == 2
    assert "--confirm-database digital_store" in capsys.readouterr().err


# ---------- с тестовой базой ----------

def test_fresh_install_and_repeat_run(make_schema):
    conn, schema = make_schema()

    first = migrate.apply_migrations(conn, log=_quiet)
    second = migrate.apply_migrations(conn, log=_quiet)

    assert [m.version for m in first] == [m.version for m in migrate.discover()]
    assert second == []
    assert _applied_versions(conn) == [m.version for m in migrate.discover()]

    _run(conn, "INSERT INTO products (name, price, quantity, is_for_sale) VALUES ('P', 1, 1, TRUE)")
    _run(conn, "INSERT INTO orders (product_id, price) VALUES (1, 1)")
    assert _query(conn, "SELECT status FROM orders") == [("pending",)]


def test_schema_sql_matches_migrations(make_schema):
    from_migrations, schema_a = make_schema()
    migrate.apply_migrations(from_migrations, log=_quiet)

    from_schema_sql, schema_b = make_schema()
    _run(from_schema_sql, (PROJECT_DIR / "schema.sql").read_text(encoding="utf-8-sig"))

    assert _structure(from_migrations, schema_a) == _structure(from_schema_sql, schema_b)


def test_old_database_without_status_keeps_data(make_schema):
    conn, schema = make_schema()
    _run(conn, OLD_STRUCTURE_SQL)
    products_before = _query(conn, LEGACY_PRODUCT_COLUMNS)

    migrate.apply_migrations(conn, log=_quiet)

    assert _query(conn, "SELECT id, product_id, price, status FROM orders") == [(1, 1, 999, "legacy")]
    assert _query(conn, LEGACY_PRODUCT_COLUMNS) == products_before

    _run(conn, "INSERT INTO orders (product_id, price) VALUES (1, 999)")
    assert _query(conn, "SELECT status FROM orders WHERE id = 2") == [("pending",)]


def test_database_with_manually_applied_001_is_adopted_without_changes(make_schema, tmp_path):
    # Проверяем именно принятие 0000–0001: на такой базе они не должны ничего менять.
    # Последующие миграции (0002+) меняют схему намеренно и проверяются отдельно.
    for name in ("0000_baseline.sql", "0001_order_status.sql"):
        shutil.copy(PROJECT_DIR / "migrations" / name, tmp_path)
    conn, schema = make_schema()
    _run(conn, OLD_STRUCTURE_SQL)
    _run(conn, MANUAL_001_SQL)
    _run(conn, "INSERT INTO orders (product_id, price, status) VALUES (1, 999, 'paid')")
    orders_before = _query(conn, "SELECT * FROM orders ORDER BY id")
    structure_before = _structure(conn, schema)

    migrate.apply_migrations(conn, tmp_path, log=_quiet)

    assert _applied_versions(conn) == ["0000", "0001"]
    assert _query(conn, "SELECT * FROM orders ORDER BY id") == orders_before
    assert _structure(conn, schema) == structure_before


def test_unexpected_existing_structure_is_rejected_and_nothing_recorded(make_schema):
    conn, schema = make_schema()
    _run(conn, "CREATE TABLE products (id SERIAL PRIMARY KEY, name TEXT NOT NULL)")

    with pytest.raises(migrate.MigrationError, match="0000_baseline.sql"):
        migrate.apply_migrations(conn, log=_quiet)

    assert not _table_exists(conn, "schema_migrations")
    assert not _table_exists(conn, "orders")


def test_failed_migration_rolls_back_the_whole_run(make_schema, tmp_path):
    shutil.copy(PROJECT_DIR / "migrations" / "0000_baseline.sql", tmp_path)
    (tmp_path / "0001_broken.sql").write_text(
        "CREATE TABLE should_not_exist (id INT);\nSELECT 1 / 0;\n", encoding="utf-8"
    )
    conn, schema = make_schema()

    with pytest.raises(migrate.MigrationError, match="0001_broken.sql"):
        migrate.apply_migrations(conn, tmp_path, log=_quiet)

    # Весь запуск — одна транзакция: откатилась и успешная 0000, и журнал.
    for table in ("should_not_exist", "products", "orders", "schema_migrations"):
        assert not _table_exists(conn, table)


def test_later_failure_does_not_touch_already_applied(make_schema, tmp_path):
    shutil.copy(PROJECT_DIR / "migrations" / "0000_baseline.sql", tmp_path)
    conn, schema = make_schema()
    migrate.apply_migrations(conn, tmp_path, log=_quiet)

    (tmp_path / "0001_broken.sql").write_text("SELECT 1 / 0;\n", encoding="utf-8")
    with pytest.raises(migrate.MigrationError, match="0001_broken.sql"):
        migrate.apply_migrations(conn, tmp_path, log=_quiet)

    assert _applied_versions(conn) == ["0000"]


def test_changed_applied_migration_is_detected(make_schema, tmp_path):
    shutil.copy(PROJECT_DIR / "migrations" / "0000_baseline.sql", tmp_path)
    conn, schema = make_schema()
    migrate.apply_migrations(conn, tmp_path, log=_quiet)

    with open(tmp_path / "0000_baseline.sql", "a", encoding="utf-8") as f:
        f.write("\nSELECT 1;\n")

    with pytest.raises(migrate.MigrationError, match="изменён после применения"):
        migrate.apply_migrations(conn, tmp_path, log=_quiet)

    assert migrate.status(conn, tmp_path)["problems"]


def test_status_does_not_write(make_schema):
    conn, schema = make_schema()

    result = migrate.status(conn)

    assert [m.version for m in result["pending"]] == [m.version for m in migrate.discover()]
    assert _query(conn, "SELECT to_regclass('schema_migrations')") == [(None,)]


def test_concurrent_runner_is_refused(make_schema):
    conn, schema = make_schema()
    other = database.get_connection()
    try:
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_lock(%s)", (migrate.LOCK_KEY,))

        with pytest.raises(migrate.MigrationError, match="другим процессом"):
            migrate.apply_migrations(conn, log=_quiet)
    finally:
        other.rollback()
        other.close()


# ---------- существующая база с правильными колонками, но неправильными ограничениями ----------

GOOD_PRODUCTS = """
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    price INTEGER NOT NULL CHECK (price >= 0),
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    is_for_sale BOOLEAN NOT NULL
"""
GOOD_ORDERS = """
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    price INTEGER NOT NULL CHECK (price >= 0)
"""
GOOD_STATUS = (
    ", status TEXT NOT NULL DEFAULT 'pending' "
    "CHECK (status IN ('pending', 'paid', 'cancelled', 'legacy'))"
)

# (products, orders, дополнительный SQL, фрагмент сообщения об ошибке)
BROKEN_DATABASES = {
    "products_without_primary_key": (
        GOOD_PRODUCTS.replace("id SERIAL PRIMARY KEY", "id SERIAL UNIQUE"),
        GOOD_ORDERS, "",
        "нет ограничения products: PRIMARY KEY (id)",
    ),
    "orders_without_primary_key": (
        GOOD_PRODUCTS,
        GOOD_ORDERS.replace("id SERIAL PRIMARY KEY", "id SERIAL NOT NULL"), "",
        "нет ограничения orders: PRIMARY KEY (id)",
    ),
    "orders_without_foreign_key": (
        GOOD_PRODUCTS,
        GOOD_ORDERS.replace(" REFERENCES products(id)", ""), "",
        "нет ограничения orders: FOREIGN KEY (product_id) REFERENCES products(id)",
    ),
    "foreign_key_on_delete_cascade": (
        GOOD_PRODUCTS,
        GOOD_ORDERS.replace("REFERENCES products(id)", "REFERENCES products(id) ON DELETE CASCADE"), "",
        "ON DELETE CASCADE",
    ),
    "foreign_key_not_valid": (
        GOOD_PRODUCTS,
        GOOD_ORDERS.replace(" REFERENCES products(id)", ""),
        "ALTER TABLE orders ADD FOREIGN KEY (product_id) REFERENCES products(id) NOT VALID;",
        "NOT VALID",
    ),
    "missing_price_check": (
        GOOD_PRODUCTS.replace("price INTEGER NOT NULL CHECK (price >= 0)", "price INTEGER NOT NULL"),
        GOOD_ORDERS, "",
        "нет ограничения products: CHECK ((price >= 0))",
    ),
    "different_quantity_check": (
        GOOD_PRODUCTS.replace("CHECK (quantity >= 0)", "CHECK (quantity > 0)"),
        GOOD_ORDERS, "",
        "лишнее или изменённое ограничение products: CHECK ((quantity > 0))",
    ),
    "extra_check": (
        GOOD_PRODUCTS, GOOD_ORDERS,
        "ALTER TABLE products ADD CHECK (price < 1000);",
        "лишнее или изменённое ограничение products: CHECK ((price < 1000))",
    ),
    "id_without_sequence_default": (
        GOOD_PRODUCTS.replace("id SERIAL PRIMARY KEY", "id INTEGER PRIMARY KEY"),
        GOOD_ORDERS, "",
        "у products.id нет DEFAULT из последовательности",
    ),
    "unexpected_default": (
        GOOD_PRODUCTS.replace("is_for_sale BOOLEAN NOT NULL", "is_for_sale BOOLEAN NOT NULL DEFAULT TRUE"),
        GOOD_ORDERS, "",
        "у products.is_for_sale неожиданный DEFAULT true",
    ),
    "status_with_wrong_check": (
        GOOD_PRODUCTS,
        GOOD_ORDERS + GOOD_STATUS.replace(", 'cancelled', 'legacy'", ""), "",
        "лишнее или изменённое ограничение orders: CHECK ((status",
    ),
    "status_without_check": (
        GOOD_PRODUCTS,
        GOOD_ORDERS + ", status TEXT NOT NULL DEFAULT 'pending'", "",
        "у orders.status нет ожидаемого CHECK",
    ),
}


def _create_existing_database(conn, products_sql, orders_sql, extra_sql=""):
    _run(conn, f"CREATE TABLE products ({products_sql}); CREATE TABLE orders ({orders_sql}); {extra_sql}")
    _run(conn, "INSERT INTO products (id, name, price, quantity, is_for_sale) VALUES (1, 'Existing', 999, 14, TRUE)")
    _run(conn, "INSERT INTO orders (id, product_id, price) VALUES (1, 1, 999)")


def _snapshot(conn, schema):
    return (
        _structure(conn, schema),
        _query(conn, "SELECT * FROM products ORDER BY id"),
        _query(conn, "SELECT * FROM orders ORDER BY id"),
        _table_exists(conn, "schema_migrations"),
    )


def test_control_correct_existing_database_is_accepted(make_schema):
    # Контроль: тот же сценарий с правильными ограничениями проходит.
    conn, schema = make_schema()
    _create_existing_database(conn, GOOD_PRODUCTS, GOOD_ORDERS)

    migrate.apply_migrations(conn, log=_quiet)

    assert _applied_versions(conn) == [m.version for m in migrate.discover()]


@pytest.mark.parametrize("case", sorted(BROKEN_DATABASES))
def test_wrong_constraints_are_rejected_and_database_unchanged(make_schema, case):
    products_sql, orders_sql, extra_sql, message = BROKEN_DATABASES[case]
    conn, schema = make_schema()
    _create_existing_database(conn, products_sql, orders_sql, extra_sql)
    before = _snapshot(conn, schema)

    with pytest.raises(migrate.MigrationError) as error:
        migrate.apply_migrations(conn, log=_quiet)

    assert message in str(error.value)
    assert "база не изменена" in str(error.value)
    assert _snapshot(conn, schema) == before
    assert not _table_exists(conn, "schema_migrations")


def test_behaviour_after_refusal(make_schema):
    products_sql, orders_sql, extra_sql, _ = BROKEN_DATABASES["missing_price_check"]
    conn, schema = make_schema()
    _create_existing_database(conn, products_sql, orders_sql, extra_sql)
    before = _snapshot(conn, schema)

    # Повторные запуски отказывают одинаково и ничего не оставляют.
    for _ in range(2):
        with pytest.raises(migrate.MigrationError, match="0000_baseline.sql"):
            migrate.apply_migrations(conn, log=_quiet)
        assert _snapshot(conn, schema) == before

    # status после отказа: всё «ожидает», проблем журнала нет, записи не появляются.
    result = migrate.status(conn)
    assert [m.version for m in result["pending"]] == [m.version for m in migrate.discover()]
    assert result["problems"] == []
    assert not _table_exists(conn, "schema_migrations")

    # Оператор исправил структуру — миграции применяются, данные целы.
    _run(conn, "ALTER TABLE products ADD CHECK (price >= 0)")
    migrate.apply_migrations(conn, log=_quiet)

    assert _applied_versions(conn) == [m.version for m in migrate.discover()]
    assert _query(conn, "SELECT id, product_id, price, status FROM orders") == [(1, 1, 999, "legacy")]
    assert _query(conn, LEGACY_PRODUCT_COLUMNS) == before[1]
