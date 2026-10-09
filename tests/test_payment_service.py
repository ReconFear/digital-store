from contextlib import contextmanager

import database
import payment_service


def test_verified_payment(monkeypatch):
    monkeypatch.setenv("DB_NAME", "digital_store_test")

    conn = database.get_connection()

    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT quantity FROM products WHERE id = 1"
            )
            quantity_before = cursor.fetchone()[0]

            cursor.execute(
                """
                INSERT INTO orders (product_id, price, status)
                VALUES (1, 999, 'pending')
                RETURNING id
                """
            )
            order_id = cursor.fetchone()[0]

        @contextmanager
        def test_connection():
            yield conn

        monkeypatch.setattr(
            payment_service,
            "get_connection",
            test_connection
        )

        result = payment_service.apply_verified_payment(order_id)

        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT quantity FROM products WHERE id = 1"
            )
            quantity_after = cursor.fetchone()[0]

            cursor.execute(
                "SELECT status FROM orders WHERE id = %s",
                (order_id,)
            )
            order_status = cursor.fetchone()[0]

        assert result["status"] == "paid"
        assert order_status == "paid"
        assert quantity_after == quantity_before - 1

    finally:
        conn.rollback()
        conn.close()