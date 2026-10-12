"""Миграция 0002 (универсальное ядро): перенос исторических данных, совместимость, защита истории.

Каждый тест — во временной схеме тестовой базы (фикстура make_schema из test_migrations.py).
"""

import pytest

import migrate
import test_migrations
from test_migrations import PROJECT_DIR, _query, _quiet, _run, _structure

# Фикстура временных схем — общая с test_migrations.py.
make_schema = test_migrations.make_schema

# База в состоянии «после 0001»: так выглядят digital_store_test и, по документам, digital_store.
STATE_0001_SQL = """
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
ALTER TABLE orders
ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'legacy'
CHECK (status IN ('pending', 'paid', 'cancelled', 'legacy'));
ALTER TABLE orders ALTER COLUMN status SET DEFAULT 'pending';
"""

HISTORY_SQL = """
INSERT INTO products (name, price, quantity, is_for_sale) VALUES
    ('Minecraft Key', 999, 14, TRUE),
    ('GTA V Key', 1499, 0, FALSE),
    ('Free sample', 0, 5, TRUE),
    ('Max price', 2147483647, 1, TRUE);
INSERT INTO orders (product_id, price, status) VALUES
    (1, 999, 'legacy'),
    (1, 899, 'paid'),
    (2, 1499, 'pending'),
    (3, 0, 'cancelled'),
    (4, 2147483647, 'pending');
"""


def _to_0001_with_history(conn):
    _run(conn, STATE_0001_SQL)
    _run(conn, HISTORY_SQL)
    migrations_0001 = [m for m in migrate.discover() if m.version <= "0001"]
    # Журнал как у базы, где 0000-0001 уже применены через migrate.py.
    _run(conn, migrate.CREATE_TABLE_SQL)
    for m in migrations_0001:
        _run(conn, f"INSERT INTO schema_migrations (version, name, checksum) "
                   f"VALUES ('{m.version}', '{m.name}', '{m.checksum}')")


@pytest.fixture
def migrated(make_schema):
    conn, schema = make_schema()
    _to_0001_with_history(conn)
    before = {
        "products": _query(conn, "SELECT id, name, price, quantity, is_for_sale FROM products ORDER BY id"),
        "orders": _query(conn, "SELECT id, product_id, price, status FROM orders ORDER BY id"),
    }
    applied = migrate.apply_migrations(conn, log=_quiet)
    return conn, before, applied


def test_only_0002_is_applied_to_a_0001_database(migrated):
    conn, before, applied = migrated

    assert [m.version for m in applied] == ["0002"]


def test_existing_products_keep_all_old_values(migrated):
    conn, before, _ = migrated

    assert _query(conn, "SELECT id, name, price, quantity, is_for_sale FROM products ORDER BY id") == before["products"]


def test_old_prices_become_kopecks_in_rub(migrated):
    conn, before, _ = migrated

    rows = _query(conn, "SELECT price, price_minor, currency FROM products ORDER BY id")
    assert rows == [(999, 99900, "RUB"), (1499, 149900, "RUB"), (0, 0, "RUB"), (2147483647, 214748364700, "RUB")]


def test_existing_products_get_conservative_types(migrated):
    conn, _, _ = migrated

    assert set(_query(conn, "SELECT product_type, inventory_mode, fulfillment_type FROM products")) == {
        ("other", "tracked", "manual")
    }


def test_existing_orders_keep_status_and_price(migrated):
    conn, before, _ = migrated

    assert _query(conn, "SELECT id, product_id, price, status FROM orders ORDER BY id") == before["orders"]


def test_each_existing_order_gets_one_item_with_its_own_historical_price(migrated):
    conn, _, _ = migrated

    items = _query(
        conn,
        """
        SELECT order_id, product_id, product_name, quantity, unit_price_minor, currency, line_total_minor
        FROM order_items ORDER BY order_id
        """,
    )
    assert items == [
        (1, 1, "Minecraft Key", 1, 99900, "RUB", 99900),
        (2, 1, "Minecraft Key", 1, 89900, "RUB", 89900),  # цена заказа, а не текущая цена товара
        (3, 2, "GTA V Key", 1, 149900, "RUB", 149900),
        (4, 3, "Free sample", 1, 0, "RUB", 0),
        (5, 4, "Max price", 1, 214748364700, "RUB", 214748364700),
    ]
    totals = _query(conn, "SELECT id, currency, total_minor FROM orders ORDER BY id")
    assert totals == [(1, "RUB", 99900), (2, "RUB", 89900), (3, "RUB", 149900), (4, "RUB", 0), (5, "RUB", 214748364700)]


def test_history_is_not_invented(migrated):
    conn, _, _ = migrated

    # Время создания старых заказов неизвестно; исполнение старых оплаченных не отслеживалось.
    assert _query(conn, "SELECT count(*) FROM orders WHERE created_at IS NOT NULL OR paid_at IS NOT NULL") == [(0,)]
    assert _query(conn, "SELECT count(*) FROM fulfillments") == [(0,)]


def test_hidden_product_stays_hidden(migrated):
    conn, _, _ = migrated

    assert _query(conn, "SELECT is_for_sale FROM products WHERE name = 'GTA V Key'") == [(False,)]


def test_fresh_install_equals_upgrade(make_schema):
    upgraded, schema_a = make_schema()
    _to_0001_with_history(upgraded)
    migrate.apply_migrations(upgraded, log=_quiet)

    fresh, schema_b = make_schema()
    migrate.apply_migrations(fresh, log=_quiet)

    assert _structure(upgraded, schema_a) == _structure(fresh, schema_b)


def test_seed_sql_still_works_after_migration(make_schema):
    conn, _ = make_schema()
    migrate.apply_migrations(conn, log=_quiet)

    _run(conn, (PROJECT_DIR / "seed.sql").read_text(encoding="utf-8-sig"))

    assert _query(conn, "SELECT id, price, price_minor, currency FROM products ORDER BY id") == [
        (1, 999, 99900, "RUB"), (2, 1499, 149900, "RUB"), (3, 1999, 199900, "RUB"),
    ]


def test_legacy_order_insert_gets_currency_total_and_item(migrated):
    conn, _, _ = migrated

    _run(conn, "INSERT INTO orders (product_id, price) VALUES (1, 999)")

    assert _query(conn, "SELECT status, currency, total_minor, created_at IS NOT NULL FROM orders WHERE id = 6") == [
        ("pending", "RUB", 99900, True)
    ]
    assert _query(conn, "SELECT quantity, unit_price_minor, line_total_minor FROM order_items WHERE order_id = 6") == [
        (1, 99900, 99900)
    ]


# ---------- цена товара: price_minor главная, price — совместимое зеркало ----------

def _product(conn, **values):
    columns = ", ".join(values)
    placeholders = ", ".join(["%s"] * len(values))
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO products ({columns}) VALUES ({placeholders}) RETURNING id, price, price_minor",
                    tuple(values.values()))
        row = cur.fetchone()
    conn.commit()
    return row


def test_kopeck_or_foreign_prices_have_no_legacy_price(migrated):
    conn, _, _ = migrated

    assert _product(conn, name="a", price_minor=12345, quantity=1, is_for_sale=True)[1:] == (None, 12345)
    assert _product(conn, name="b", price_minor=50000, quantity=1, is_for_sale=True)[1:] == (500, 50000)
    assert _product(conn, name="c", price_minor=50000, currency="USD", quantity=1, is_for_sale=True)[1:] == (None, 50000)


def test_legacy_price_update_keeps_both_fields_consistent(migrated):
    conn, _, _ = migrated

    _run(conn, "UPDATE products SET price = 500 WHERE id = 1")
    assert _query(conn, "SELECT price, price_minor FROM products WHERE id = 1") == [(500, 50000)]
    _run(conn, "UPDATE products SET price_minor = 77750 WHERE id = 1")
    assert _query(conn, "SELECT price, price_minor FROM products WHERE id = 1") == [(None, 77750)]


@pytest.mark.parametrize(
    "values",
    [
        dict(name="x", price=10, price_minor=999, quantity=1, is_for_sale=True),                 # противоречие
        dict(name="x", price=10, currency="USD", quantity=1, is_for_sale=True),                  # рубли в USD
        dict(name="x", price_minor=-1, quantity=1, is_for_sale=True),
        dict(name="x", price_minor=1_000_000_000_001, quantity=1, is_for_sale=True),
        dict(name="x", price_minor=100, currency="rub", quantity=1, is_for_sale=True),
        dict(name="x", quantity=1, is_for_sale=True),                                            # нет цены
        dict(name="x", price_minor=100, quantity=1, is_for_sale=True, product_type="game"),
        dict(name="x", price_minor=100, quantity=1, is_for_sale=True, product_type="physical",
             inventory_mode="unlimited"),
        dict(name="x", price_minor=100, quantity=1, is_for_sale=True, product_type="digital_file",
             fulfillment_type="physical_shipping"),
        dict(name="x", price_minor=100, quantity=5, is_for_sale=True, product_type="service",
             inventory_mode="unlimited"),                                                        # unlimited => quantity 0
    ],
)
def test_invalid_products_are_rejected_by_database(migrated, values):
    conn, _, _ = migrated

    with pytest.raises(Exception):
        _product(conn, **values)
    conn.rollback()


@pytest.mark.parametrize(
    "product_type, inventory_mode, fulfillment_type",
    [
        ("digital_key", "tracked", "digital_keys"),
        ("digital_file", "unlimited", "downloads"),
        ("digital_file", "tracked", "downloads"),
        ("physical", "tracked", "physical_shipping"),
        ("service", "unlimited", "manual"),
        ("service", "tracked", "manual"),
        ("other", "tracked", "manual"),
    ],
)
def test_supported_product_kinds(migrated, product_type, inventory_mode, fulfillment_type):
    conn, _, _ = migrated
    quantity = 0 if inventory_mode == "unlimited" else 3

    _product(conn, name="ok", price_minor=100, quantity=quantity, is_for_sale=True,
             product_type=product_type, inventory_mode=inventory_mode, fulfillment_type=fulfillment_type)


# ---------- защита истории ----------

def test_order_items_are_immutable(migrated):
    conn, _, _ = migrated

    with pytest.raises(Exception, match="неизменяемы"):
        _run(conn, "UPDATE order_items SET unit_price_minor = 1 WHERE order_id = 1")
    conn.rollback()


def test_order_money_is_immutable_but_status_can_change(migrated):
    conn, _, _ = migrated

    with pytest.raises(Exception, match="неизменяемы"):
        _run(conn, "UPDATE orders SET total_minor = 1 WHERE id = 3")
    conn.rollback()
    _run(conn, "UPDATE orders SET status = 'cancelled' WHERE id = 3")
    assert _query(conn, "SELECT status FROM orders WHERE id = 3") == [("cancelled",)]


def test_changing_product_price_or_name_does_not_change_existing_orders(migrated):
    conn, _, _ = migrated

    _run(conn, "UPDATE products SET price_minor = 1, name = 'Renamed' WHERE id = 1")

    assert _query(conn, "SELECT product_name, unit_price_minor FROM order_items WHERE order_id = 1") == [
        ("Minecraft Key", 99900)
    ]
    assert _query(conn, "SELECT price, total_minor FROM orders WHERE id = 1") == [(999, 99900)]


def test_new_status_requires_action_is_allowed_and_unknown_is_not(migrated):
    conn, _, _ = migrated

    _run(conn, "UPDATE orders SET status = 'requires_action' WHERE id = 3")
    with pytest.raises(Exception):
        _run(conn, "UPDATE orders SET status = 'shipped' WHERE id = 3")
    conn.rollback()
