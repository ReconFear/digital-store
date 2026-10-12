"""Схема запроса POST /api/v2/orders (pydantic, без FastAPI и базы)."""

import pytest
from pydantic import ValidationError

from commerce.money import MAX_ITEM_QUANTITY, MAX_ORDER_ITEMS
from commerce.schemas import CreateOrderIn


def test_valid_request():
    request = CreateOrderIn.model_validate({"items": [{"product_id": 1, "quantity": 2}, {"product_id": 3, "quantity": 1}]})

    assert [(i.product_id, i.quantity) for i in request.items] == [(1, 2), (3, 1)]


@pytest.mark.parametrize(
    "payload",
    [
        {"items": []},
        {},
        {"items": [{"product_id": 1}]},
        {"items": [{"quantity": 1}]},
        {"items": [{"product_id": 0, "quantity": 1}]},
        {"items": [{"product_id": -1, "quantity": 1}]},
        {"items": [{"product_id": 1, "quantity": 0}]},
        {"items": [{"product_id": 1, "quantity": -3}]},
        {"items": [{"product_id": 1, "quantity": MAX_ITEM_QUANTITY + 1}]},
        {"items": [{"product_id": 1, "quantity": "2"}]},
        {"items": [{"product_id": "1", "quantity": 2}]},
        {"items": [{"product_id": 1, "quantity": 1.5}]},
        {"items": [{"product_id": 1, "quantity": True}]},
        {"items": [{"product_id": 2**31, "quantity": 1}]},
        {"items": [{"product_id": i, "quantity": 1} for i in range(1, MAX_ORDER_ITEMS + 2)]},
    ],
)
def test_invalid_requests_are_rejected(payload):
    with pytest.raises(ValidationError):
        CreateOrderIn.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"items": [{"product_id": 1, "quantity": 1, "price": 1}]},
        {"items": [{"product_id": 1, "quantity": 1, "unit_price_minor": 1}]},
        {"items": [{"product_id": 1, "quantity": 1}], "total": 1},
        {"items": [{"product_id": 1, "quantity": 1}], "status": "paid"},
        {"items": [{"product_id": 1, "quantity": 1}], "currency": "USD"},
    ],
)
def test_client_cannot_send_prices_totals_currency_or_status(payload):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        CreateOrderIn.model_validate(payload)
