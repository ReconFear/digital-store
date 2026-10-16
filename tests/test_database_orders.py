"""Заказ v1 на товар без остатка — «unavailable». Свой товар, откат в конце."""

from database import create_order_in_db


def test_unavailable_product_in_postgresql(shared_test_connection, v1_product):
    product_id = v1_product(price=999, quantity=0)

    result = create_order_in_db(product_id)

    assert result == {"error": "unavailable"}


def test_product_not_for_sale_is_unavailable(shared_test_connection, v1_product):
    product_id = v1_product(price=999, quantity=3, is_for_sale=False)

    assert create_order_in_db(product_id) == {"error": "unavailable"}
