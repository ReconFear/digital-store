"""HTTP-маршруты оплаты (FastAPI TestClient + тестовая база + имитатор ЮKassa).

Требует FastAPI (как test_api.py и test_api_v2.py).
"""

import pytest
from fastapi.testclient import TestClient

import api
from http_security import RequestLimitsMiddleware, SlidingWindowLimiter
from payment_testkit import (  # noqa: F401
    clean_events,
    notification,
    pay_settings,
    payment_row,
    provider,
    scalar,
    service,
    yookassa,
)


@pytest.fixture
def client():
    return TestClient(api.app)


@pytest.fixture
def paying_client(service, monkeypatch):
    monkeypatch.setattr(api, "payments_v2", service)
    return TestClient(api.app)


def _order(client, make_product, quantity=1, price_minor=25000):
    pid = make_product(price_minor=price_minor)
    response = client.post("/api/v2/orders", json={"items": [{"product_id": pid, "quantity": quantity}]})
    assert response.status_code == 201
    return response.json()


def test_v2_order_returns_access_token_once_and_order_shape_is_unchanged(client, make_product):
    body = _order(client, make_product)

    assert set(body) == {"order", "access_token"}
    assert set(body["order"]) == {"id", "status", "created_at", "total", "items"}
    assert len(body["access_token"]) == 43
    assert body["access_token"] not in str(scalar("SELECT token_sha256 FROM order_access WHERE order_id = %s",
                                                  (body["order"]["id"],)))


def test_status_requires_the_order_secret(client, make_product):
    mine = _order(client, make_product)
    other = _order(client, make_product)
    url = f"/api/v2/orders/{mine['order']['id']}/status"

    ok = client.post(url, json={"access_token": mine["access_token"]})
    assert ok.status_code == 200
    assert ok.json()["order"]["id"] == mine["order"]["id"]
    assert (ok.json()["payment_status"], ok.json()["fulfillment_status"]) == ("none", "not_started")

    for token in (other["access_token"], "x" * 43, "short"):
        denied = client.post(url, json={"access_token": token})
        assert denied.status_code == 404
        assert denied.json() == {"detail": {"code": "order_not_found", "message": "Заказ не найден."}}
    assert client.post("/api/v2/orders/999999999/status", json={"access_token": mine["access_token"]}).status_code \
        == 404
    assert client.post(url + f"?access_token={mine['access_token']}", json={}).status_code == 422
    assert client.post(url, json={"access_token": mine["access_token"], "status": "paid"}).status_code == 422
    assert client.get(url).status_code == 405


def test_checkout_is_disabled_by_default(client, make_product):
    body = _order(client, make_product)
    url = f"/api/v2/orders/{body['order']['id']}/checkout"
    response = client.post(url, json={"access_token": body["access_token"]})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "payments_disabled"


def test_checkout_returns_provider_page_and_does_not_pay(paying_client, yookassa, make_product):
    body = _order(paying_client, make_product, quantity=2, price_minor=25000)
    order_id = body["order"]["id"]

    response = paying_client.post(f"/api/v2/orders/{order_id}/checkout", json={"access_token": body["access_token"]})

    assert response.status_code == 200
    assert response.json()["payment_status"] == "awaiting_payment"
    assert response.json()["confirmation_url"].startswith("https://")
    assert payment_row(order_id)[0][3] == 50000
    assert scalar("SELECT status FROM orders WHERE id = %s", (order_id,)) == "pending"
    # Клиент не может прислать сумму: лишние поля запрещены.
    assert paying_client.post(f"/api/v2/orders/{order_id}/checkout",
                              json={"access_token": body["access_token"], "amount": 1}).status_code == 422


def test_webhook_queues_and_never_pays_by_itself(paying_client, yookassa, make_product, service):
    body = _order(paying_client, make_product)
    order_id = body["order"]["id"]
    paying_client.post(f"/api/v2/orders/{order_id}/checkout", json={"access_token": body["access_token"]})
    provider_id = payment_row(order_id)[0][2]

    response = paying_client.post("/api/v2/payments/yookassa/webhook",
                                  content=notification("payment.succeeded", provider_id),
                                  headers={"Content-Type": "application/json"})
    assert response.status_code == 200
    assert scalar("SELECT status FROM orders WHERE id = %s", (order_id,)) == "pending"
    assert scalar("SELECT status FROM provider_events WHERE object_id = %s", (provider_id,)) == "received"

    assert paying_client.post("/api/v2/payments/yookassa/webhook", content=b"nonsense").status_code == 400


def test_webhook_is_not_found_while_payments_disabled(client):
    assert client.post("/api/v2/payments/yookassa/webhook",
                       content=notification("payment.succeeded", "abc")).status_code == 404


def test_webhook_database_failure_is_reported_not_hidden(paying_client, monkeypatch, service):
    def broken(body, ip):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(service, "accept_notification", broken)
    client = TestClient(api.app, raise_server_exceptions=False)
    assert client.post("/api/v2/payments/yookassa/webhook",
                       content=notification("payment.succeeded", "abc")).status_code == 500


def test_rate_limit_and_body_size_on_real_app(make_product):
    limited = TestClient(RequestLimitsMiddleware(api.app, limiter=SlidingWindowLimiter()))
    pid = make_product()
    codes = [limited.post("/api/v2/orders", json={"items": [{"product_id": pid, "quantity": 1}]}).status_code
             for _ in range(11)]
    assert codes[:10] == [201] * 10 and codes[10] == 429

    big = {"items": [{"product_id": pid, "quantity": 1}], "pad": "x" * 20_000}
    other_client = TestClient(RequestLimitsMiddleware(api.app, limiter=SlidingWindowLimiter()))
    assert other_client.post("/api/v2/orders", json=big).status_code == 413


def test_old_routes_and_absence_of_order_reading_are_unchanged(client):
    assert client.get("/api/v2/orders/1").status_code == 404
    assert client.post("/api/v2/orders/1/pay").status_code == 404
    assert client.get("/orders").status_code == 403
    assert "/api/v2/payments/yookassa/webhook" not in client.get("/openapi.json").json()["paths"]


def test_checkout_under_review_is_409_without_link(paying_client, yookassa, make_product):
    body = _order(paying_client, make_product)
    order_id = body["order"]["id"]
    yookassa.create_overrides = {"test": False}             # противоречивый ответ на создание
    first = paying_client.post(f"/api/v2/orders/{order_id}/checkout", json={"access_token": body["access_token"]})
    assert first.status_code == 503 and "confirmation_url" not in first.text
    yookassa.create_overrides = {}
    second = paying_client.post(f"/api/v2/orders/{order_id}/checkout", json={"access_token": body["access_token"]})
    assert second.status_code == 409
    assert second.json()["detail"]["code"] == "payment_under_review"
