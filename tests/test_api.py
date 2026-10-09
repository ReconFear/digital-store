from fastapi.testclient import TestClient
import api


def test_get_products(monkeypatch):
    test_products = [
        {
            "id": 1,
            "name": "Test Game",
            "price": 500,
            "quantity": 3,
            "is_for_sale": True
        }
    ]

    monkeypatch.setattr(
        api,
        "get_products_from_db",
        lambda: test_products
    )

    client = TestClient(api.app)
    response = client.get("/products")

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["products"][0]["name"] == "Test Game"


def test_get_product_not_found(monkeypatch):
    monkeypatch.setattr(
        api,
        "get_products_from_db",
        lambda: []
    )

    client = TestClient(api.app)
    response = client.get("/products/999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Product not found"


def test_create_order_success(monkeypatch):
    def fake_create_order(product_id):
        return {
            "id": 10,
            "product_id": product_id,
            "price": 999
        }

    monkeypatch.setattr(
        api,
        "create_order_in_db",
        fake_create_order
    )

    client = TestClient(api.app)

    response = client.post(
        "/orders",
        json={"product_id": 1}
    )

    assert response.status_code == 200
    assert response.json()["message"] == "Order created"
    assert response.json()["order"]["product_id"] == 1
    assert response.json()["order"]["price"] == 999


def test_create_order_not_found(monkeypatch):
    def fake_create_order(product_id):
        return {"error": "not_found"}

    monkeypatch.setattr(
        api,
        "create_order_in_db",
        fake_create_order
    )

    client = TestClient(api.app)

    response = client.post(
        "/orders",
        json={"product_id": 999}
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Product not found"