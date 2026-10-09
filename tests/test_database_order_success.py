from contextlib import contextmanager

import database


def test_successful_order_with_rollback(monkeypatch):
    monkeypatch.setenv("DB_NAME", "digital_store_test")

    real_get_connection = database.get_connection

    @contextmanager
    def rollback_connection():
        conn = real_get_connection()

        try:
            yield conn
        finally:
            conn.rollback()
            conn.close()

    monkeypatch.setattr(
        database,
        "get_connection",
        rollback_connection
    )

    result = database.create_order_in_db(1)

    assert result["product_id"] == 1
    assert result["price"] == 999
    assert "id" in result