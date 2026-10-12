"""Денежная модель Digital Store.

* Все суммы — целые числа в минимальных единицах валюты (копейки для RUB).
  float не используется нигде: ни в расчётах, ни в хранении.
* Валюта хранится явно рядом с каждой суммой (ISO 4217, три заглавные буквы).
* Границы выбраны так, чтобы сумма заказа гарантированно помещалась в BIGINT
  PostgreSQL и в безопасный диапазон целых чисел JavaScript (2^53 - 1):
      цена единицы  <= 1 000 000 000 000  (10 млрд RUB)
      количество    <= 1 000 в позиции
      позиций       <= 50 в заказе
      итог заказа   <= 50 * 1000 * 10^12 = 5 * 10^16 — больше 2^53, поэтому итог
                       дополнительно ограничен MAX_ORDER_TOTAL_MINOR = 10^15.
"""

# Валюты, которые магазин умеет продавать: код -> число знаков после запятой.
# Новая валюта = новая строка здесь (и проверка округления/налогов для неё).
# Товары в валютах вне списка не показываются в каталоге v2 и не продаются.
CURRENCY_EXPONENTS = {
    "RUB": 2,
}

MAX_UNIT_PRICE_MINOR = 1_000_000_000_000
MAX_ITEM_QUANTITY = 1000
MAX_ORDER_ITEMS = 50
MAX_ORDER_TOTAL_MINOR = 1_000_000_000_000_000


class MoneyError(ValueError):
    """Недопустимая денежная величина."""


def _require_int(value, name):
    # bool — подкласс int в Python, но деньгами и количеством быть не может.
    if type(value) is not int:
        raise MoneyError(f"{name} должно быть целым числом, получено {type(value).__name__}")


def is_supported_currency(currency):
    return currency in CURRENCY_EXPONENTS


def line_total_minor(unit_price_minor, quantity):
    """Итог строки заказа с проверкой границ."""
    _require_int(unit_price_minor, "unit_price_minor")
    _require_int(quantity, "quantity")
    if not 0 <= unit_price_minor <= MAX_UNIT_PRICE_MINOR:
        raise MoneyError(f"цена единицы вне допустимого диапазона: {unit_price_minor}")
    if not 1 <= quantity <= MAX_ITEM_QUANTITY:
        raise MoneyError(f"количество вне допустимого диапазона: {quantity}")
    return unit_price_minor * quantity


def order_total_minor(line_totals):
    """Итог заказа с проверкой верхней границы."""
    total = 0
    for value in line_totals:
        _require_int(value, "line_total_minor")
        if value < 0:
            raise MoneyError("итог строки не может быть отрицательным")
        total += value
    if total > MAX_ORDER_TOTAL_MINOR:
        raise MoneyError("итог заказа превышает допустимый максимум")
    return total


def money_json(amount_minor, currency):
    """Представление суммы в API v2: {"amount_minor", "currency", "exponent"}."""
    _require_int(amount_minor, "amount_minor")
    if not is_supported_currency(currency):
        raise MoneyError(f"валюта {currency!r} не поддерживается")
    return {"amount_minor": amount_minor, "currency": currency, "exponent": CURRENCY_EXPONENTS[currency]}
