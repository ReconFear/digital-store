"""Платёжный модуль Digital Store (PR 3). Архитектура — PAYMENTS.md."""

from payments.config import load_payment_settings
from payments.service import PaymentService, PaymentServiceError
from payments.yookassa import YooKassaProvider


def build_payment_service(settings=None):
    """Сервис по настройкам окружения. При выключенных платежах провайдер не создаётся."""
    settings = settings or load_payment_settings()
    provider = YooKassaProvider.from_settings(settings) if settings.enabled else _DisabledProvider()
    return PaymentService(provider, settings)


class _DisabledProvider:
    name = "yookassa"

    def create_payment(self, **kwargs):
        raise RuntimeError("платежи выключены (PAYMENTS_ENABLED=false)")

    def get_payment(self, provider_payment_id):
        raise RuntimeError("платежи выключены (PAYMENTS_ENABLED=false)")


__all__ = ["PaymentService", "PaymentServiceError", "YooKassaProvider", "build_payment_service",
           "load_payment_settings"]
