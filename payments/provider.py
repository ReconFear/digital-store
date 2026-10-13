"""Интерфейс платёжного провайдера (заменяемый адаптер).

Платёжный модуль работает только через PaymentProvider. Реальная реализация —
payments/yookassa.py. Другой провайдер = новый класс с тем же интерфейсом.

Ошибки разделены по смыслу для денег, а не по HTTP-кодам:
* ProviderUnavailable — результат НЕИЗВЕСТЕН (таймаут, обрыв, 5xx, исчерпаны повторы).
  Платёж мог быть создан. Повторять можно только с тем же ключом идемпотентности.
* ProviderRejected   — провайдер точно отклонил запрос (4xx), платёж не создан.
* ProviderAuthError  — неверные учётные данные магазина (401/403), платёж не создан.
* ProviderNotFound   — объекта у провайдера нет (404): например, поддельный ID в уведомлении.
* ProviderProtocolError — ответ не соответствует протоколу; данным ответа не доверяем.
"""

import re
from dataclasses import dataclass, field
from typing import Protocol

from commerce.money import CURRENCY_EXPONENTS, MAX_ORDER_TOTAL_MINOR

# Валюты, в которых провайдер МОЖЕТ сообщить полученные деньги (ISO 4217 -> знаков после запятой).
# Это шире списка валют продажи (commerce.money.CURRENCY_EXPONENTS): если деньги пришли не в
# валюте заказа, их нужно честно учесть в той валюте, в какой они пришли (currency_mismatch),
# а не застрять на ошибке разбора. Валюта вне списка — ошибка протокола, учёт не делается.
PROVIDER_CURRENCY_EXPONENTS = {
    "RUB": 2, "USD": 2, "EUR": 2, "BYN": 2, "KZT": 2, "UAH": 2, "UZS": 2, "CNY": 2, "GBP": 2,
    "CHF": 2, "TRY": 2, "AMD": 2, "GEL": 2, "AZN": 2, "KGS": 2, "TJS": 2, "MDL": 2, "JPY": 0,
}
PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PROVIDER_STATUSES = frozenset({"pending", "waiting_for_capture", "succeeded", "canceled"})


class ProviderError(Exception):
    """Базовая ошибка провайдера. Сообщение не содержит секретов и ответов провайдера целиком."""


class ProviderUnavailable(ProviderError):
    """Результат запроса неизвестен."""


class ProviderRejected(ProviderError):
    def __init__(self, code, http_status):
        super().__init__(f"провайдер отклонил запрос: HTTP {http_status}, код {code}")
        self.code = code
        self.http_status = http_status


class ProviderAuthError(ProviderError):
    """Учётные данные магазина не приняты."""


class ProviderNotFound(ProviderError):
    """Объект у провайдера не найден."""


class ProviderProtocolError(ProviderError):
    """Ответ провайдера не соответствует ожидаемому формату."""


@dataclass(frozen=True)
class ProviderPayment:
    """Состояние платежа, полученное ОТ ПРОВАЙДЕРА серверным запросом."""

    id: str
    status: str
    amount_minor: int
    currency: str
    test: bool
    paid: bool
    account_id: str | None
    confirmation_url: str | None = None
    cancellation_reason: str | None = None
    metadata: dict = field(default_factory=dict)


class PaymentProvider(Protocol):
    name: str

    def create_payment(self, *, idempotence_key, amount_minor, currency, description,
                       return_url, metadata) -> ProviderPayment: ...

    def get_payment(self, provider_payment_id) -> ProviderPayment: ...

    def find_payment_by_metadata(self, *, payment_attempt, order_id, created_gte,
                                 created_lt) -> ProviderPayment | None: ...


# ---------- точное преобразование сумм (без float) ----------

_DECIMAL_RE = re.compile(r"^(0|[1-9][0-9]{0,17})(?:\.([0-9]{1,6}))?$")


def minor_to_decimal_string(amount_minor, currency):
    """12345, RUB -> "123.45". Только целые; валюта — из списка поддерживаемых."""
    if type(amount_minor) is not int or not 0 < amount_minor <= MAX_ORDER_TOTAL_MINOR:
        raise ValueError("сумма платежа должна быть целым положительным числом минимальных единиц")
    exponent = CURRENCY_EXPONENTS.get(currency)
    if exponent is None:
        raise ValueError(f"валюта {currency!r} не поддерживается")
    if exponent == 0:
        return str(amount_minor)
    units, minor = divmod(amount_minor, 10 ** exponent)
    return f"{units}.{minor:0{exponent}d}"


def decimal_string_to_minor(value, currency):
    """"123.45", RUB -> 12345. Лишние ненулевые знаки после запятой — ошибка протокола."""
    exponent = PROVIDER_CURRENCY_EXPONENTS.get(currency)
    if exponent is None:
        raise ProviderProtocolError("провайдер вернул неподдерживаемую валюту")
    if not isinstance(value, str):
        raise ProviderProtocolError("сумма от провайдера должна быть строкой")
    match = _DECIMAL_RE.match(value)
    if not match:
        raise ProviderProtocolError("сумма от провайдера в неверном формате")
    units, fraction = match.group(1), match.group(2) or ""
    if len(fraction) > exponent:
        if fraction[exponent:].strip("0"):
            raise ProviderProtocolError("сумма от провайдера точнее минимальной единицы валюты")
        fraction = fraction[:exponent]
    return int(units) * 10 ** exponent + int(fraction.ljust(exponent, "0") or "0")
