"""Повторное применение оплаты не списывает остаток дважды. Свой товар, откат в конце."""

from contextlib import contextmanager

import payment_service


def _quantity(conn, product_id):
    with conn.cursor() as cursor:
        cursor.execute("SELECT quantity FROM products WHERE id = %s", (product_id,))
        return cursor.fetchone()[0]


def test_payment_is_not_applied_twice(monkeypatch, shared_test_connection, v1_product):
    conn = shared_test_connection
    product_id = v1_product(price=999, quantity=5)
    quantity_before = _quantity(conn, product_id)

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

    first_result = payment_service.apply_verified_payment(order_id)
    second_result = payment_service.apply_verified_payment(order_id)

    assert first_result["status"] == "paid"
    assert second_result["status"] == "already_paid"
    assert _quantity(conn, product_id) == quantity_before - 1
