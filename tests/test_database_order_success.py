"""Заказ v1 на свой товар; всё откатывается (shared_test_connection)."""

import database


def test_successful_order_with_rollback(shared_test_connection, v1_product):
    product_id = v1_product(price=999, quantity=5)

    result = database.create_order_in_db(product_id)

    assert result["product_id"] == product_id
    assert result["price"] == 999
    assert result["status"] == "pending"
    assert "id" in result
