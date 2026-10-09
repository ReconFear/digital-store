from contextlib import contextmanager

import database


def test_pending_order_does_not_reduce_stock(monkeypatch):
    monkeypatch.setenv("DB_NAME", "digital_store_test")

    real_get_connection = database.get_connection
    quantities = {}

    @contextmanager
    def test_connection():
        conn = real_get_connection()

        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT quantity FROM products WHERE id = 1"
                )
                quantities["before"] = cursor.fetchone()[0]

            yield conn

            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT quantity FROM products WHERE id = 1"
                )
                quantities["after"] = cursor.fetchone()[0]

        finally:
            conn.rollback()
            conn.close()

    monkeypatch.setattr(
        database,
        "get_connection",
        test_connection
    )

    result = database.create_order_in_db(1)

    assert result["status"] == "pending"
    assert quantities["before"] == quantities["after"]