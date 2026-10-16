"""HTTP-контракт API v2 и неизменность v1 (FastAPI TestClient + реальная тестовая база).

Данные создаются в одной транзакции и откатываются (фикстура shared_test_connection).
"""

import pytest
from fastapi.testclient import TestClient

import api
from cors_settings import get_cors_origins


@pytest.fixture
def client(shared_test_connection):
    return TestClient(api.app)


def _product(conn, *, name="API v2", price_minor=12345, quantity=10, is_for_sale=True,
             product_type="other", inventory_mode="tracked", fulfillment_type="manual"):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO products (name, price_minor, quantity, is_for_sale, product_type, inventory_mode, fulfillment_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """,
            (name, price_minor, quantity, is_for_sale, product_type, inventory_mode, fulfillment_type),
        )
        return cursor.fetchone()[0]


def test_create_order_v2_returns_201_and_server_computed_totals(client, shared_test_connection):
    a = _product(shared_test_connection, name="A", price_minor=12345)
    b = _product(shared_test_connection, name="B", price_minor=100, quantity=0, product_type="service",
                 inventory_mode="unlimited")

    response = client.post("/api/v2/orders", json={"items": [{"product_id": a, "quantity": 2},
                                                              {"product_id": b, "quantity": 3}]})

    assert response.status_code == 201
    order = response.json()["order"]
    assert set(order) == {"id", "status", "created_at", "total", "items"}
    assert order["status"] == "pending"
    assert order["total"] == {"amount_minor": 12345 * 2 + 300, "currency": "RUB", "exponent": 2}
    assert [(i["product_id"], i["quantity"], i["line_total"]["amount_minor"]) for i in order["items"]] == [
        (a, 2, 24690), (b, 3, 300)
    ]


@pytest.mark.parametrize("payload", [
    {"items": []},
    {"items": [{"product_id": 1, "quantity": 0}]},
    {"items": [{"product_id": 1, "quantity": "1"}]},
    {"items": [{"product_id": 1, "quantity": 1, "price": 1}]},
    {"items": [{"product_id": 1, "quantity": 1}], "status": "paid"},
])
def test_create_order_v2_rejects_invalid_input_with_422(client, payload):
    response = client.post("/api/v2/orders", json=payload)

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_create_order_v2_business_errors_have_structured_detail(client, shared_test_connection):
    hidden = _product(shared_test_connection, is_for_sale=False)
    scarce = _product(shared_test_connection, quantity=1)

    not_found = client.post("/api/v2/orders", json={"items": [{"product_id": hidden, "quantity": 1}]})
    short = client.post("/api/v2/orders", json={"items": [{"product_id": scarce, "quantity": 2}]})

    assert not_found.status_code == 404
    assert not_found.json()["detail"]["code"] == "product_not_found"
    assert not_found.json()["detail"]["product_ids"] == [hidden]
    assert short.status_code == 409
    assert short.json()["detail"] == {
        "code": "insufficient_stock", "message": "Недостаточно товара в наличии.", "product_ids": [scarce]
    }


def test_v2_catalog(client, shared_test_connection):
    pid = _product(shared_test_connection, name="Catalog v2", price_minor=50050)

    listing = client.get("/api/v2/products").json()
    one = client.get(f"/api/v2/products/{pid}")

    assert listing["total"] == len(listing["products"])
    assert one.status_code == 200
    assert one.json()["price"] == {"amount_minor": 50050, "currency": "RUB", "exponent": 2}
    assert client.get("/api/v2/products/2").status_code == 404  # GTA V из seed скрыт
    assert client.get("/api/v2/products/0").status_code == 404


def test_there_is_no_way_to_read_or_pay_orders_over_http(client):
    assert client.get("/api/v2/orders").status_code == 405
    assert client.get("/api/v2/orders/1").status_code == 404
    assert client.post("/api/v2/orders/1/pay").status_code == 404
    assert client.get("/orders").status_code == 403


def test_v1_contract_is_unchanged(client, v1_product):
    # Свой товар в транзакции shared_test_connection (откатывается), а не товар № 1 из seed.sql.
    product_id = v1_product(price=999, quantity=3)
    products = client.get("/products").json()
    created = client.post("/orders", json={"product_id": product_id})

    assert set(products) == {"products", "total"}
    assert all(set(p) == {"id", "name", "price", "quantity", "is_for_sale"} for p in products["products"])
    assert created.status_code == 200
    assert set(created.json()["order"]) == {"id", "product_id", "price", "status"}
    assert created.json()["order"]["status"] == "pending"


def test_cors_preflight_for_v2_orders(client):
    origin = get_cors_origins()[0]

    response = client.options("/api/v2/orders", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
