"""Применение подтверждённой оплаты. Свой товар, откат в конце."""

from contextlib import contextmanager

import payment_service


def test_verified_payment(monkeypatch, shared_test_connection, v1_product):
    conn = shared_test_connection
    product_id = v1_product(price=999, quantity=5)

    with conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO orders (product_id, price, status) VALUES (%s, 999, 'pending') RETURNING id",
            (product_id,),
        )
        order_id = cursor.fetchone()[0]

    @contextmanager
    def test_connection():
        yield conn

    monkeypatch.setattr(payment_service, "get_connection", test_connection)

    result = payment_service.apply_verified_payment(order_id)

    with conn.cursor() as cursor:
        cursor.execute("SELECT quantity FROM products WHERE id = %s", (product_id,))
        quantity_after = cursor.fetchone()[0]
        cursor.execute("SELECT status FROM orders WHERE id = %s", (order_id,))
        order_status = cursor.fetchone()[0]

    assert result["status"] == "paid"
    assert order_status == "paid"
    assert quantity_after == 4
