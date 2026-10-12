"""Денежная модель (commerce/money.py). Без базы и без FastAPI."""

import pytest

from commerce.money import (
    MAX_ITEM_QUANTITY,
    MAX_ORDER_TOTAL_MINOR,
    MAX_UNIT_PRICE_MINOR,
    MoneyError,
    line_total_minor,
    money_json,
    order_total_minor,
)


def test_line_total_is_exact_integer_arithmetic():
    assert line_total_minor(99900, 3) == 299700
    assert line_total_minor(1, MAX_ITEM_QUANTITY) == 1000
    # 0.1 + 0.2 != 0.3 во float — здесь float не участвует вообще
    assert line_total_minor(10, 1) + line_total_minor(20, 1) == 30


def test_free_item_is_allowed():
    assert line_total_minor(0, 5) == 0


@pytest.mark.parametrize("unit, qty", [(-1, 1), (MAX_UNIT_PRICE_MINOR + 1, 1), (100, 0), (100, -1),
                                       (100, MAX_ITEM_QUANTITY + 1)])
def test_line_total_rejects_out_of_range(unit, qty):
    with pytest.raises(MoneyError):
        line_total_minor(unit, qty)


@pytest.mark.parametrize("unit, qty", [(99.9, 1), (100, 1.0), (True, 1), (100, True), ("100", 1)])
def test_line_total_rejects_non_integers_and_bool(unit, qty):
    with pytest.raises(MoneyError):
        line_total_minor(unit, qty)


def test_order_total_sums_lines_and_has_upper_bound():
    assert order_total_minor([99900, 199900, 0]) == 299800
    assert order_total_minor([]) == 0
    with pytest.raises(MoneyError):
        order_total_minor([MAX_ORDER_TOTAL_MINOR, 1])
    with pytest.raises(MoneyError):
        order_total_minor([-1])


def test_maximum_order_fits_bigint_and_javascript_safe_integer():
    assert MAX_ORDER_TOTAL_MINOR < 2**53 - 1 < 2**63 - 1
    assert MAX_UNIT_PRICE_MINOR * MAX_ITEM_QUANTITY <= MAX_ORDER_TOTAL_MINOR


def test_money_json_has_explicit_currency_and_exponent():
    assert money_json(99900, "RUB") == {"amount_minor": 99900, "currency": "RUB", "exponent": 2}
    with pytest.raises(MoneyError):
        money_json(100, "XXX")
    with pytest.raises(MoneyError):
        money_json(1.5, "RUB")
