"""Заказ «pending» не уменьшает остаток. Свой товар, откат в конце."""

import database


def _quantity(conn, product_id):
    with conn.cursor() as cursor:
        cursor.execute("SELECT quantity FROM products WHERE id = %s", (product_id,))
        return cursor.fetchone()[0]


def test_pending_order_does_not_reduce_stock(shared_test_connection, v1_product):
    product_id = v1_product(price=999, quantity=5)
    before = _quantity(shared_test_connection, product_id)

    result = database.create_order_in_db(product_id)

    assert result["status"] == "pending"
    assert _quantity(shared_test_connection, product_id) == before == 5
