"""Адаптер ЮKassa и настройки платежей: протокол, повторы, ошибки, секреты.

Адаптер настоящий (urllib, HTTP), провайдер — локальный имитатор tests/yookassa_mock.py.
База данных не нужна.
"""

import base64
import json
import logging

import pytest

from payment_testkit import SECRET, SHOP_ID, pay_settings, provider, yookassa  # noqa: F401
from payments.config import LIVE_CONFIRMATION, PaymentConfigError, PaymentSettings, load_payment_settings
from payments.provider import (
    ProviderAuthError,
    ProviderNotFound,
    ProviderProtocolError,
    ProviderRejected,
    ProviderUnavailable,
    decimal_string_to_minor,
    minor_to_decimal_string,
)
from payments.yookassa import YooKassaProvider


def _create(provider, key="2f3c7b0e-1d4a-4c8e-9f00-000000000001", amount=10000):
    return provider.create_payment(idempotence_key=key, amount_minor=amount, currency="RUB",
                                   description="Заказ №1", return_url="https://shop.example/return",
                                   metadata={"order_id": "1", "payment_attempt": "1"})


# ---------- суммы ----------

@pytest.mark.parametrize("minor, text", [(1, "0.01"), (10, "0.10"), (100, "1.00"), (12345, "123.45"),
                                         (10 ** 15, "10000000000000.00")])
def test_amount_conversion_is_exact_both_ways(minor, text):
    assert minor_to_decimal_string(minor, "RUB") == text
    assert decimal_string_to_minor(text, "RUB") == minor


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "100", 10 ** 15 + 1])
def test_invalid_amounts_are_not_sent(bad):
    with pytest.raises(ValueError):
        minor_to_decimal_string(bad, "RUB")


@pytest.mark.parametrize("bad", ["1.001", "1e3", "-1.00", " 1.00", "01.00", 100, "1,00", ""])
def test_invalid_amounts_from_provider_are_protocol_errors(bad):
    with pytest.raises(ProviderProtocolError):
        decimal_string_to_minor(bad, "RUB")


def test_trailing_zero_fraction_from_provider_is_accepted():
    assert decimal_string_to_minor("2.000", "RUB") == 200


# ---------- протокол ----------

def test_create_payment_request_matches_protocol(provider, yookassa):
    payment = _create(provider)

    method, path, headers, body = yookassa.requests[0]
    assert (method, path) == ("POST", "/v3/payments")
    assert headers["authorization"] == "Basic " + base64.b64encode(f"{SHOP_ID}:{SECRET}".encode()).decode()
    assert headers["idempotence-key"] == "2f3c7b0e-1d4a-4c8e-9f00-000000000001"
    assert headers["content-type"] == "application/json"
    assert json.loads(body)["amount"] == {"value": "100.00", "currency": "RUB"}
    assert (payment.status, payment.paid, payment.amount_minor, payment.test) == ("pending", False, 10000, True)
    assert payment.account_id == SHOP_ID
    assert payment.confirmation_url.startswith("https://")


def test_same_key_returns_same_payment(provider, yookassa):
    assert _create(provider).id == _create(provider).id
    assert len(yookassa.payments) == 1


def test_get_payment_reads_current_state(provider, yookassa):
    payment = _create(provider)
    yookassa.set_status(payment.id, "succeeded")

    current = provider.get_payment(payment.id)
    assert (current.status, current.paid, current.amount_minor) == ("succeeded", True, 10000)
    assert yookassa.requests[-1][:2] == ("GET", f"/v3/payments/{payment.id}")


@pytest.mark.parametrize("mode", ["timeout", "500", "500_after_processing", "202", "429", "drop"])
def test_transient_failure_is_retried_with_same_key(provider, yookassa, mode):
    yookassa.fail_next(mode, method="POST")
    payment = _create(provider)

    posts = yookassa.created_payments()
    assert len(posts) == 2
    assert posts[0][2]["idempotence-key"] == posts[1][2]["idempotence-key"]
    assert len(yookassa.payments) == 1 and payment.id in yookassa.payments


def test_exhausted_retries_report_unknown_outcome(provider, yookassa):
    yookassa.fail_next("500_after_processing", count=3, method="POST")
    with pytest.raises(ProviderUnavailable):
        _create(provider)
    assert len(yookassa.created_payments()) == 3
    assert len(yookassa.payments) == 1   # платёж мог появиться — поэтому «неизвестно», а не «отказ»


@pytest.mark.parametrize("mode, error", [("401", ProviderAuthError), ("400", ProviderRejected),
                                         ("garbage", ProviderProtocolError)])
def test_definite_failures_are_not_retried(provider, yookassa, mode, error):
    yookassa.fail_next(mode, method="POST")
    with pytest.raises(error):
        _create(provider)
    assert len(yookassa.created_payments()) == 1


def test_unknown_payment_is_not_found(provider):
    with pytest.raises(ProviderNotFound):
        provider.get_payment("does-not-exist")


@pytest.mark.parametrize("bad_id", ["../payments", "a/b", "", "x" * 65, None, 5])
def test_payment_id_is_validated_before_request(provider, yookassa, bad_id):
    with pytest.raises(ProviderNotFound):
        provider.get_payment(bad_id)
    assert yookassa.requests == []


def test_response_with_invalid_fields_is_rejected(provider, yookassa):
    payment = _create(provider)
    for field, value in (("status", "paid"), ("test", "yes"), ("id", "../x")):
        original = yookassa.payments[payment.id][field]
        yookassa.payments[payment.id][field] = value
        with pytest.raises(ProviderProtocolError):
            provider.get_payment(payment.id)
        yookassa.payments[payment.id][field] = original


def test_plain_http_is_refused_except_loopback():
    with pytest.raises(ValueError):
        YooKassaProvider(shop_id="1", secret_key="test_x", api_url="http://api.yookassa.ru/v3")
    YooKassaProvider(shop_id="1", secret_key="test_x", api_url="https://api.yookassa.ru/v3")
    YooKassaProvider(shop_id="1", secret_key="test_x", api_url="http://127.0.0.1:9/v3")


# ---------- секреты (14) ----------

def test_secret_never_appears_in_repr_errors_or_logs(provider, yookassa, pay_settings, caplog):
    caplog.set_level(logging.DEBUG)
    auth = base64.b64encode(f"{SHOP_ID}:{SECRET}".encode()).decode()
    texts = [repr(provider), repr(pay_settings), str(pay_settings)]
    for mode in ("401", "400", "garbage"):
        yookassa.fail_next(mode, method="POST")
        with pytest.raises(Exception) as error:
            _create(provider)
        texts.append(str(error.value))
        texts.append(repr(error.value))
    yookassa.fail_next("500", count=3, method="POST")
    with pytest.raises(ProviderUnavailable) as error:
        _create(provider)
    texts.append(str(error.value))
    texts.append(caplog.text)

    joined = "\n".join(texts)
    assert SECRET not in joined
    assert auth not in joined


# ---------- настройки ----------

def _env(**extra):
    base = {"PAYMENTS_ENABLED": "true", "YOOKASSA_SHOP_ID": "123456",
            "YOOKASSA_SECRET_KEY": "test_abcdefgh12345678", "PAYMENT_RETURN_URL": "https://shop.example/return"}
    base.update(extra)
    return {k: v for k, v in base.items() if v is not None}


def test_payments_are_disabled_by_default():
    assert load_payment_settings({}).enabled is False
    assert PaymentSettings().enabled is False


def test_test_mode_is_default_and_requires_test_key():
    settings = load_payment_settings(_env())
    assert (settings.enabled, settings.mode, settings.expect_test_payments) == (True, "test", True)
    with pytest.raises(PaymentConfigError, match="не тестовый"):
        load_payment_settings(_env(YOOKASSA_SECRET_KEY="live_abcdefgh12345678"))


def test_live_mode_requires_explicit_confirmation_and_live_key():
    with pytest.raises(PaymentConfigError, match="живые платежи выключены"):
        load_payment_settings(_env(YOOKASSA_MODE="live", YOOKASSA_SECRET_KEY="live_abcdefgh12345678"))
    with pytest.raises(PaymentConfigError, match="не боевой"):
        load_payment_settings(_env(YOOKASSA_MODE="live", PAYMENTS_ALLOW_LIVE=LIVE_CONFIRMATION))
    settings = load_payment_settings(_env(YOOKASSA_MODE="live", PAYMENTS_ALLOW_LIVE=LIVE_CONFIRMATION,
                                          YOOKASSA_SECRET_KEY="live_abcdefgh12345678"))
    assert settings.mode == "live" and settings.expect_test_payments is False


@pytest.mark.parametrize("extra", [
    {"YOOKASSA_SHOP_ID": "abc"}, {"YOOKASSA_SECRET_KEY": None}, {"PAYMENT_RETURN_URL": None},
    {"PAYMENT_RETURN_URL": "http://shop.example/return"}, {"YOOKASSA_API_URL": "http://api.yookassa.ru/v3"},
    {"PAYMENT_RETURN_URL": "https://user:pass@shop.example/"}, {"YOOKASSA_TIMEOUT_SECONDS": "0"},
    {"PAYMENTS_PROVIDER": "other"}, {"PAYMENTS_ENABLED": "maybe"},
])
def test_invalid_configuration_is_refused(extra):
    with pytest.raises(PaymentConfigError):
        load_payment_settings(_env(**extra))


def test_config_errors_do_not_contain_the_secret():
    secret = "test_abc$%^&*()"
    with pytest.raises(PaymentConfigError) as error:
        load_payment_settings(_env(YOOKASSA_SECRET_KEY=secret))
    assert secret not in str(error.value)


def test_env_example_has_no_secrets_and_keeps_payments_disabled():
    from test_migrations import PROJECT_DIR
    values = {}
    for line in (PROJECT_DIR / ".env.example").read_text(encoding="utf-8-sig").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    assert values.get("PAYMENTS_ENABLED") == "false"
    assert load_payment_settings(values).enabled is False
    assert values.get("DB_PASSWORD", "") == ""
    assert "YOOKASSA_SECRET_KEY" not in values
    text = (PROJECT_DIR / ".env.example").read_text(encoding="utf-8-sig")
    import re
    assert re.search(r"(test|live)_[A-Za-z0-9_-]{8,}", text) is None   # нет ничего похожего на ключ ЮKassa
