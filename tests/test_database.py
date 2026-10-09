from database import get_products_from_db


def test_get_products_from_postgresql(monkeypatch):
    monkeypatch.setenv("DB_NAME", "digital_store_test")

    products = get_products_from_db()

    assert len(products) == 3
    assert products[0]["name"] == "Minecraft Key"
    assert products[1]["name"] == "GTA V Key"
    assert products[2]["name"] == "Cyberpunk 2077"