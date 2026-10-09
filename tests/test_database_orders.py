from database import create_order_in_db


def test_unavailable_product_in_postgresql(monkeypatch):
    monkeypatch.setenv("DB_NAME", "digital_store_test")

    result = create_order_in_db(2)

    assert result == {"error": "unavailable"}