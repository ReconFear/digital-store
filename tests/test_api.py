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
        "get_public_products_from_db",
        lambda available_only=False: test_products
    )

    client = TestClient(api.app)
    response = client.get("/products")

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["products"][0]["name"] == "Test Game"


def test_get_products_passes_available_only(monkeypatch):
    calls = []

    def fake_public_products(available_only=False):
        calls.append(available_only)
        return []

    monkeypatch.setattr(
        api,
        "get_public_products_from_db",
        fake_public_products
    )

    client = TestClient(api.app)

    assert client.get("/products").status_code == 200
    assert client.get("/products?available_only=true").status_code == 200
    assert calls == [False, True]


def test_get_product_not_found(monkeypatch):
    monkeypatch.setattr(
        api,
        "get_public_product_from_db",
        lambda product_id: None
    )

    client = TestClient(api.app)
    response = client.get("/products/999")

    assert response.status_code == 404
    assert response.json()["detail"] == "Product not found"


def test_get_product_found(monkeypatch):
    product = {
        "id": 7,
        "name": "Test Product",
        "price": 100,
        "quantity": 1,
        "is_for_sale": True
    }

    monkeypatch.setattr(
        api,
        "get_public_product_from_db",
        lambda product_id: product if product_id == 7 else None
    )

    client = TestClient(api.app)
    response = client.get("/products/7")

    assert response.status_code == 200
    assert response.json() == product


def test_api_does_not_use_full_product_list():
    # Полный список (со скрытыми товарами) не должен быть доступен публичному API.
    assert not hasattr(api, "get_products_from_db")


def test_create_order_success(monkeypatch):
    def fake_create_order(product_id):
        return {
            "id": 10,
            "product_id": product_id,
            "price": 999,
            "status": "pending"
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
    assert response.json()["order"]["status"] == "pending"


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
