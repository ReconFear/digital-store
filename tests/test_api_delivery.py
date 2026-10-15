"""PR5: маршрут POST /api/v2/orders/{id}/delivery через FastAPI TestClient (нужен установленный FastAPI).

Секреты — тестовые: get_delivery получает тестовую связку ключей через подмену default_keyring."""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402
from commerce.digital_keys import delivery as delivery_module  # noqa: E402
from digital_keys_testkit import delivery_keyring, make_digital, pay, run_worker  # noqa: E402

NO_STORE = {"cache-control": "no-store", "pragma": "no-cache", "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer"}

client = TestClient(api.app)


@pytest.fixture(autouse=True)
def _test_keyring(monkeypatch):
    monkeypatch.setattr(delivery_module, "default_keyring", delivery_keyring)
    # Значение по умолчанию параметра get_delivery вычислено при импорте — подменяем и его.
    monkeypatch.setattr(delivery_module.get_delivery, "__defaults__", (delivery_keyring,))


def _create(pid, quantity=1):
    response = client.post("/api/v2/orders", json={"items": [{"product_id": pid, "quantity": quantity}]})
    assert response.status_code == 201, response.text
    body = response.json()
    return body["order"]["id"], body["access_token"]


def _headers_ok(response):
    return all(response.headers.get(k) == v for k, v in NO_STORE.items())


def test_full_contract(make_product):
    pid, codes = make_digital(make_product, keys=2)
    order_id, token = _create(pid, 2)

    response = client.post(f"/api/v2/orders/{order_id}/delivery", json={"access_token": token})
    assert response.status_code == 200 and _headers_ok(response)
    assert response.json()["items"][0]["delivery_status"] == "awaiting_payment"

    pay(order_id)
    assert client.post(f"/api/v2/orders/{order_id}/delivery", json={"access_token": token}).json()[
        "items"][0]["delivery_status"] == "processing"
    run_worker()
    body = client.post(f"/api/v2/orders/{order_id}/delivery", json={"access_token": token}).json()
    item = body["items"][0]
    assert body["order_status"] == "paid" and item["delivery_status"] == "delivered"
    assert sorted(item["digital_keys"]) == sorted(codes)
    assert set(item) == {"order_item_id", "product_id", "name", "fulfillment_type", "quantity", "delivery_status",
                         "digital_keys"}


@pytest.mark.parametrize("body", [{"access_token": "x" * 43}, {"access_token": ""}])
def test_wrong_token_is_404_with_no_store(make_product, body):
    pid, _ = make_digital(make_product, keys=1)
    order_id, _ = _create(pid)
    response = client.post(f"/api/v2/orders/{order_id}/delivery", json=body)
    assert response.status_code in (404, 422) and _headers_ok(response)
    if response.status_code == 404:
        assert response.json()["detail"]["code"] == "order_not_found"


def test_extra_fields_and_token_in_query_are_rejected(make_product):
    pid, _ = make_digital(make_product, keys=1)
    order_id, token = _create(pid)
    response = client.post(f"/api/v2/orders/{order_id}/delivery", json={"access_token": token, "x": 1})
    assert response.status_code == 422 and _headers_ok(response)


def test_503_when_keyring_is_missing(make_product, monkeypatch):
    from commerce.digital_keys.crypto import Keyring
    pid, codes = make_digital(make_product, keys=1)
    order_id, token = _create(pid)
    pay(order_id)
    run_worker()
    monkeypatch.setattr(delivery_module.get_delivery, "__defaults__", (lambda: Keyring.from_env({}),))
    response = client.post(f"/api/v2/orders/{order_id}/delivery", json={"access_token": token})
    assert response.status_code == 503 and _headers_ok(response)
    assert response.json()["detail"]["code"] == "delivery_unavailable" and codes[0] not in response.text
