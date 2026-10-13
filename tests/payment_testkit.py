"""Общие фикстуры тестов платежей: имитатор ЮKassa, настоящий адаптер, сервис.

Импортируется тестовыми модулями: `from payment_testkit import *` (фикстуры pytest).
"""

import pytest

import database
from payments.access import create_order_with_access
from payments.config import PaymentSettings
from payments.service import PaymentService
from payments.yookassa import YooKassaProvider
from yookassa_mock import YooKassaMock

SHOP_ID = "123456"
SECRET = "test_mock_secret_key_0001"


@pytest.fixture
def yookassa():
    mock = YooKassaMock(shop_id=SHOP_ID, secret_key=SECRET).start()
    yield mock
    mock.stop()


@pytest.fixture
def pay_settings(yookassa):
    return PaymentSettings(
        enabled=True, provider="yookassa", shop_id=SHOP_ID, secret_key=SECRET, mode="test",
        api_url=yookassa.url, return_url="https://shop.example/return", timeout_seconds=0.5,
        max_attempts=3, webhook_ip_check=False, confirmation_hosts=("yoomoney.mock",),
    )


@pytest.fixture
def provider(pay_settings):
    return YooKassaProvider.from_settings(pay_settings, sleep=lambda seconds: None)


@pytest.fixture
def service(provider, pay_settings, clean_events):
    return PaymentService(provider, pay_settings)


@pytest.fixture
def clean_events():
    """Удаляет события провайдера, появившиеся за время теста (таблица без защиты удаления)."""
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT coalesce(max(id), 0) FROM provider_events")
            start = cursor.fetchone()[0]
    yield
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM provider_events WHERE id > %s", (start,))


@pytest.fixture
def new_order(make_product):
    """new_order(price_minor=..., quantity=..., stock=...) -> (order, token, product_id)."""

    def factory(price_minor=12345, quantity=1, stock=10, **product):
        pid = make_product(price_minor=price_minor, quantity=stock, **product)
        order, token = create_order_with_access([(pid, quantity)])
        return order, token, pid

    return factory


def notification(event, object_id):
    import json
    return json.dumps({"type": "notification", "event": event,
                       "object": {"id": object_id, "status": "succeeded", "paid": True,
                                  "amount": {"value": "1.00", "currency": "RUB"}}}).encode()


def payment_row(order_id):
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, status, provider_payment_id, amount_minor, currency, status_reason, idempotence_key::text
                FROM payments WHERE order_id = %s ORDER BY id
                """,
                (order_id,),
            )
            return cursor.fetchall()


def scalar(sql, params=None):
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchone()[0]
