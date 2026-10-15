"""Создание многопозиционного заказа, каталоги v1/v2 и совместимость v1 (реальная тестовая база).

Все данные создаются внутри одной транзакции и откатываются (фикстура shared_test_connection).
"""

import pytest

import database
from commerce import catalog
from commerce.orders import OrderError, create_order
from digital_keys_testkit import stock_keys


def _product(conn, *, name="PR2 product", price_minor=10000, currency="RUB", quantity=10,
             is_for_sale=True, product_type="other", inventory_mode="tracked", fulfillment_type="manual"):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO products (name, price_minor, currency, quantity, is_for_sale,
                                  product_type, inventory_mode, fulfillment_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (name, price_minor, currency, quantity, is_for_sale, product_type, inventory_mode, fulfillment_type),
        )
        return cursor.fetchone()[0]


def _one(conn, sql, params=None):
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchone()


def _all(conn, sql, params=None):
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


def _orders_count(conn):
    return _one(conn, "SELECT count(*) FROM orders")[0]


# ---------- многопозиционный заказ ----------

def test_order_with_several_products_is_computed_on_server(shared_test_connection):
    conn = shared_test_connection
    # PR5: остаток товара с автоматической выдачей обеспечен ключами (5 ключей -> quantity 5).
    key = _product(conn, name="Key", price_minor=99900, quantity=0, product_type="digital_key",
                   fulfillment_type="digital_keys")
    stock_keys(key, 5, conn=conn)
    book = _product(conn, name="E-book", price_minor=49950, quantity=0, product_type="digital_file",
                    inventory_mode="unlimited", fulfillment_type="downloads")
    consult = _product(conn, name="Consultation", price_minor=300000, quantity=0, product_type="service",
                       inventory_mode="unlimited")

    order = create_order([(key, 2), (book, 3), (consult, 1)])

    assert order["status"] == "pending"
    assert order["total"] == {"amount_minor": 99900 * 2 + 49950 * 3 + 300000, "currency": "RUB", "exponent": 2}
    assert [(i["product_id"], i["quantity"], i["unit_price"]["amount_minor"], i["line_total"]["amount_minor"])
            for i in order["items"]] == [(key, 2, 99900, 199800), (book, 3, 49950, 149850), (consult, 1, 300000, 300000)]

    stored = _one(conn, "SELECT status, currency, total_minor, product_id, price FROM orders WHERE id = %s", (order["id"],))
    assert stored == ("pending", "RUB", 649650, None, None)  # v2-заказ не имеет устаревших колонок
    items = _all(conn, "SELECT product_id, product_name, product_type, fulfillment_type, quantity, unit_price_minor, "
                       "line_total_minor FROM order_items WHERE order_id = %s ORDER BY id", (order["id"],))
    assert items == [
        (key, "Key", "digital_key", "digital_keys", 2, 99900, 199800),
        (book, "E-book", "digital_file", "downloads", 3, 49950, 149850),
        (consult, "Consultation", "service", "manual", 1, 300000, 300000),
    ]


def test_creating_order_does_not_change_stock(shared_test_connection):
    conn = shared_test_connection
    pid = _product(conn, quantity=5)

    create_order([(pid, 3)])

    assert _one(conn, "SELECT quantity FROM products WHERE id = %s", (pid,))[0] == 5


def test_duplicate_items_are_merged(shared_test_connection):
    conn = shared_test_connection
    a = _product(conn, name="A", price_minor=100)
    b = _product(conn, name="B", price_minor=250)

    order = create_order([(a, 1), (b, 2), (a, 3)])

    assert [(i["product_id"], i["quantity"]) for i in order["items"]] == [(a, 4), (b, 2)]
    assert order["total"]["amount_minor"] == 4 * 100 + 2 * 250


def test_merged_quantity_is_still_limited(shared_test_connection):
    pid = _product(shared_test_connection, quantity=5000)

    with pytest.raises(OrderError) as error:
        create_order([(pid, 600), (pid, 600)])

    assert error.value.code == "quantity_too_large"
    assert error.value.http_status == 422


def test_unknown_and_hidden_products_are_not_found_and_nothing_is_created(shared_test_connection):
    conn = shared_test_connection
    ok = _product(conn)
    hidden = _product(conn, is_for_sale=False)
    before = _orders_count(conn)

    with pytest.raises(OrderError) as error:
        create_order([(ok, 1), (hidden, 1), (2_000_000_000, 1)])

    assert error.value.code == "product_not_found"
    assert error.value.http_status == 404
    assert error.value.product_ids == [hidden, 2_000_000_000]
    assert _orders_count(conn) == before


def test_insufficient_stock_is_rejected(shared_test_connection):
    conn = shared_test_connection
    plenty = _product(conn, quantity=10)
    scarce = _product(conn, quantity=2)
    before = _orders_count(conn)

    with pytest.raises(OrderError) as error:
        create_order([(plenty, 1), (scarce, 3)])

    assert (error.value.code, error.value.http_status, error.value.product_ids) == ("insufficient_stock", 409, [scarce])
    assert _orders_count(conn) == before


def test_unlimited_products_ignore_quantity(shared_test_connection):
    pid = _product(shared_test_connection, quantity=0, product_type="digital_file",
                   inventory_mode="unlimited", fulfillment_type="downloads")

    assert create_order([(pid, 1000)])["total"]["amount_minor"] == 1000 * 10000


def test_currencies_cannot_be_mixed(shared_test_connection):
    conn = shared_test_connection
    rub = _product(conn, currency="RUB")
    usd = _product(conn, currency="USD")

    with pytest.raises(OrderError) as mixed:
        create_order([(rub, 1), (usd, 1)])
    with pytest.raises(OrderError) as unsupported:
        create_order([(usd, 1)])

    assert (mixed.value.code, mixed.value.http_status) == ("mixed_currency", 409)
    assert (unsupported.value.code, unsupported.value.http_status) == ("unsupported_currency", 409)


def test_order_keeps_historical_price_and_name(shared_test_connection):
    conn = shared_test_connection
    pid = _product(conn, name="Old name", price_minor=50000)
    order = create_order([(pid, 2)])

    with conn.cursor() as cursor:
        cursor.execute("UPDATE products SET price_minor = 99999, name = 'New name' WHERE id = %s", (pid,))

    assert _one(conn, "SELECT product_name, unit_price_minor, line_total_minor FROM order_items WHERE order_id = %s",
                (order["id"],)) == ("Old name", 50000, 100000)
    assert _one(conn, "SELECT total_minor FROM orders WHERE id = %s", (order["id"],))[0] == 100000


def test_empty_order_is_rejected():
    with pytest.raises(OrderError) as error:
        create_order([])

    assert error.value.code == "empty_order"


# ---------- совместимость v1 ----------

def test_v1_order_is_stored_in_the_universal_model(shared_test_connection):
    conn = shared_test_connection
    # Свой товар формата v1 (не товар № 1 из seed.sql): цена 999 ₽ = 99900 копеек.
    pid = _product(conn, name="V1 product", price_minor=99900, quantity=5)

    result = database.create_order_in_db(pid)

    assert set(result) == {"id", "product_id", "price", "status"}
    assert (result["product_id"], result["price"], result["status"]) == (pid, 999, "pending")
    assert _one(conn, "SELECT currency, total_minor FROM orders WHERE id = %s", (result["id"],)) == ("RUB", 99900)
    assert _all(conn, "SELECT product_id, quantity, unit_price_minor FROM order_items WHERE order_id = %s",
                (result["id"],)) == [(pid, 1, 99900)]


def test_v1_catalog_shows_only_products_it_can_describe(shared_test_connection):
    conn = shared_test_connection
    legacy_like = _product(conn, name="Whole rubles", price_minor=50000)
    kopecks = _product(conn, name="With kopecks", price_minor=50050)
    unlimited = _product(conn, name="Unlimited", quantity=0, product_type="digital_file",
                         inventory_mode="unlimited", fulfillment_type="downloads")
    usd = _product(conn, name="USD", currency="USD")

    v1_ids = {p["id"] for p in database.get_public_products_from_db()}

    assert legacy_like in v1_ids
    assert {kopecks, unlimited, usd}.isdisjoint(v1_ids)
    assert database.get_public_product_from_db(kopecks) is None
    assert database.create_order_in_db(unlimited) == {"error": "not_found"}
    # Формат ответа v1 не изменился.
    assert set(database.get_public_products_from_db()[0]) == {"id", "name", "price", "quantity", "is_for_sale"}


def test_v2_catalog_format_and_visibility(shared_test_connection):
    conn = shared_test_connection
    tracked = _product(conn, name="Tracked", price_minor=50050, quantity=0)
    unlimited = _product(conn, name="Unlimited", quantity=0, product_type="service", inventory_mode="unlimited")
    hidden = _product(conn, name="Hidden", is_for_sale=False)
    usd = _product(conn, name="USD", currency="USD")

    by_id = {p["id"]: p for p in catalog.get_public_products()}

    assert by_id[tracked] == {
        "id": tracked, "name": "Tracked", "product_type": "other", "inventory_mode": "tracked",
        "price": {"amount_minor": 50050, "currency": "RUB", "exponent": 2},
        "stock_quantity": 0, "available": False,
    }
    assert by_id[unlimited]["stock_quantity"] is None and by_id[unlimited]["available"] is True
    assert hidden not in by_id and usd not in by_id
    assert "fulfillment_type" not in by_id[tracked]

    available = {p["id"] for p in catalog.get_public_products(available_only=True)}
    assert unlimited in available and tracked not in available
    assert catalog.get_public_product(hidden) is None
    assert catalog.get_public_product(unlimited)["id"] == unlimited


def test_seed_products_look_the_same_in_v1_and_v2(shared_test_connection):
    v1 = {p["id"]: p for p in database.get_public_products_from_db()}
    v2 = {p["id"]: p for p in catalog.get_public_products()}

    for product_id, old in v1.items():
        new = v2[product_id]
        assert new["price"]["amount_minor"] == old["price"] * 100
        assert new["stock_quantity"] == old["quantity"]
