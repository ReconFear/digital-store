"""PR 3 rev3: продолжение «потерянного» платежа, найденного у провайдера в статусе pending,
и возвраты в валюте фактически полученных денег.

Настоящий адаптер ЮKassa -> локальный имитатор по HTTP (не настоящая ЮKassa),
настоящая тестовая база PostgreSQL.
"""

import pytest

import database
from payment_testkit import (  # noqa: F401  (фикстуры pytest)
    clean_events,
    new_order,
    notification,
    pay_settings,
    payment_row,
    provider,
    scalar,
    service,
    yookassa,
)
from payments.service import PaymentServiceError
from test_payments_rev2 import _lose_attempt, _run


def _row(order_id):
    return _run("SELECT status, status_reason, provider_payment_id, confirmation_url FROM payments "
                "WHERE order_id = %s ORDER BY id", (order_id,))


def _find_lost(service):
    service.stale_attempt_grace_seconds = 0
    service.unresolved_recheck_seconds = 0
    service.open_payment_recheck_seconds = 0
    return service.reconcile()


# ================= 1. найденный pending-платёж можно безопасно продолжить =================

def test_lost_response_after_24h_pending_payment_is_recovered_with_link_and_paid_once(service, yookassa, new_order):
    order, token, pid = new_order(price_minor=25000, quantity=2, stock=5)
    _lose_attempt(service, yookassa, order, token, "500_after_processing")   # ответ на создание потерян
    lost = next(iter(yookassa.payments))
    posts = len(yookassa.created_payments())

    with pytest.raises(PaymentServiceError) as error:                       # прошли сутки: новой ссылки нет
        service.checkout(order["id"], token)
    assert error.value.code == "payment_under_review"

    assert _find_lost(service)["discovered"] == 1                          # найден в списке ЮKassa (pending)
    link = yookassa.payments[lost]["confirmation"]["confirmation_url"]
    assert _row(order["id"]) == [("pending", None, lost, link)]

    result = service.checkout(order["id"], token)                          # покупатель продолжает тот же платёж
    assert result == {"payment_status": "awaiting_payment", "confirmation_url": link}
    status = service.order_status(order["id"], token)
    assert (status["payment_status"], status["confirmation_url"]) == ("awaiting_payment", link)
    assert len(yookassa.created_payments()) == posts and len(yookassa.payments) == 1

    yookassa.set_status(lost, "succeeded")                                 # оплатил по восстановленной ссылке
    service.accept_notification(notification("payment.succeeded", lost), "x")
    assert [r[1] for r in service.process_due_events()] == ["succeeded"]

    assert scalar("SELECT status FROM orders WHERE id = %s", (order["id"],)) == "paid"
    assert scalar("SELECT quantity FROM products WHERE id = %s", (pid,)) == 3   # списано один раз
    assert _run("SELECT amount_minor, currency FROM money_operations WHERE order_id = %s",
                (order["id"],)) == [(50000, "RUB")]
    assert scalar("SELECT count(*) FROM refunds WHERE order_id = %s", (order["id"],)) == 0
    assert scalar("SELECT count(*) FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id "
                  "WHERE i.order_id = %s", (order["id"],)) == 1
    with pytest.raises(PaymentServiceError) as again:
        service.checkout(order["id"], token)
    assert again.value.code == "order_not_payable"
    assert len(yookassa.payments) == 1
    service.accept_notification(notification("payment.succeeded", lost), "x")   # повтор ничего не списывает
    service.process_due_events()
    assert scalar("SELECT quantity FROM products WHERE id = %s", (pid,)) == 3


@pytest.mark.parametrize("confirmation", [
    None,
    {"type": "redirect", "confirmation_url": "http://yoomoney.mock/pay"},
    {"type": "redirect", "confirmation_url": "https://evil.example/pay"},
    {"type": "redirect"},
])
def test_recovered_pending_without_valid_link_is_honest_requires_action(service, yookassa, new_order, confirmation):
    order, token, _ = new_order()
    _lose_attempt(service, yookassa, order, token, "500_after_processing")
    lost = next(iter(yookassa.payments))
    if confirmation is None:
        yookassa.payments[lost].pop("confirmation")
    else:
        yookassa.payments[lost]["confirmation"] = confirmation

    _find_lost(service)

    assert _row(order["id"]) == [("requires_action", "unresolved_attempt", lost, None)]
    status = service.order_status(order["id"], token)
    assert (status["payment_status"], status["confirmation_url"]) == ("requires_action", None)
    with pytest.raises(PaymentServiceError) as error:
        service.checkout(order["id"], token)
    assert error.value.code == "payment_under_review"                     # не awaiting_payment без ссылки
    assert scalar("SELECT count(*) FROM audit_log WHERE order_id = %s AND action = 'payment.unresolved_checked'",
                  (order["id"],)) == 1

    # Повторная сверка: тот же результат, журнал не засоряется.
    _find_lost(service)
    assert _row(order["id"])[0][0] == "requires_action"
    assert scalar("SELECT count(*) FROM audit_log WHERE order_id = %s AND action = 'payment.unresolved_checked'",
                  (order["id"],)) == 1

    # Провайдер отменил неоплаченный платёж -> попытка закрыта, оплату можно начать заново.
    yookassa.set_status(lost, "canceled")
    _find_lost(service)
    assert _row(order["id"])[0][0] == "canceled"
    result = service.checkout(order["id"], token)
    assert result["confirmation_url"].startswith("https://yoomoney.mock/")
    assert [r[0] for r in _row(order["id"])] == ["canceled", "pending"]


def test_recovered_link_is_kept_only_after_lost_payment_is_paid_by_buyer_who_has_it(service, yookassa, new_order):
    # Найденный без ссылки платёж всё же оплачен (ссылка была у покупателя) — учёт обычный, один раз.
    order, token, pid = new_order()
    _lose_attempt(service, yookassa, order, token, "500_after_processing")
    lost = next(iter(yookassa.payments))
    yookassa.payments[lost]["confirmation"] = {"type": "redirect", "confirmation_url": "http://bad/"}
    _find_lost(service)
    yookassa.set_status(lost, "succeeded")
    _find_lost(service)
    assert _row(order["id"])[0][:3] == ("succeeded", None, lost)
    assert scalar("SELECT status FROM orders WHERE id = %s", (order["id"],)) == "paid"
    assert scalar("SELECT quantity FROM products WHERE id = %s", (pid,)) == 9


@pytest.mark.parametrize("change, reason", [
    ({"amount": "1.00"}, "amount_mismatch"),
    ({"currency": "USD"}, "currency_mismatch"),
    ({"account_id": "999999"}, "shop_mismatch"),
    ({"test": False}, "test_mode_mismatch"),
    ({"paid": True}, "provider_state_inconsistent"),
    ({"metadata_order": True}, None),          # чужой заказ в metadata — попытка не найдётся вовсе
])
def test_recovered_pending_payment_is_checked_before_link_is_stored(service, yookassa, new_order, change, reason):
    order, token, _ = new_order(price_minor=10000)
    _lose_attempt(service, yookassa, order, token, "500_after_processing")
    lost = next(iter(yookassa.payments))
    if "metadata_order" in change:
        yookassa.payments[lost]["metadata"]["order_id"] = str(order["id"] + 1)
    else:
        yookassa.tamper(lost, **change)

    _find_lost(service)

    status, status_reason, provider_id, link = _row(order["id"])[0]
    assert status == "requires_action" and link is None
    if reason:
        assert (status_reason, provider_id) == (reason, lost)
    else:
        assert (status_reason, provider_id) == ("unresolved_attempt", None)
    with pytest.raises(PaymentServiceError) as error:
        service.checkout(order["id"], token)
    assert error.value.code == "payment_under_review"
    assert scalar("SELECT count(*) FROM money_operations WHERE order_id = %s", (order["id"],)) == 0


# ================= 2. возврат в валюте фактически полученных денег =================

def _paid_in(service, yookassa, new_order, currency, value):
    order, token, pid = new_order(price_minor=10000)
    service.checkout(order["id"], token)
    provider_id = payment_row(order["id"])[0][2]
    yookassa.set_status(provider_id, "succeeded")
    yookassa.tamper(provider_id, currency=currency, amount=value)
    service.accept_notification(notification("payment.succeeded", provider_id), "x")
    results = [r[1] for r in service.process_due_events()]
    return order, token, pid, payment_row(order["id"])[0][0], results


@pytest.mark.parametrize("value, minor, reason", [
    ("100.00", 10000, "currency_mismatch"),     # та же сумма, другая валюта
    ("12.34", 1234, "amount_mismatch"),         # и сумма другая — сумма проверяется первой
])
def test_money_in_other_currency_is_recorded_and_refund_is_requested_in_that_currency(
        service, yookassa, new_order, value, minor, reason):
    order, token, pid, payment_id, results = _paid_in(service, yookassa, new_order, "USD", value)

    assert results == ["requires_action"]
    assert payment_row(order["id"])[0][5] == reason
    assert scalar("SELECT status FROM orders WHERE id = %s", (order["id"],)) == "pending"
    assert scalar("SELECT quantity FROM products WHERE id = %s", (pid,)) == 10
    assert _run("SELECT kind, amount_minor, currency FROM money_operations WHERE payment_id = %s",
                (payment_id,)) == [("payment", minor, "USD")]
    # Автоматический запрос возврата — в долларах, на полученную сумму, и только запрос.
    assert _run("SELECT amount_minor, currency, status, reason FROM refunds WHERE payment_id = %s",
                (payment_id,)) == [(minor, "USD", "requested", reason)]
    assert scalar("SELECT count(*) FROM money_operations WHERE payment_id = %s AND kind = 'refund'",
                  (payment_id,)) == 0
    assert service.order_status(order["id"], token)["refund_status"] == "pending"


def test_manual_refund_request_uses_received_currency_and_amount(service, yookassa, new_order):
    order, _, _, payment_id, _ = _paid_in(service, yookassa, new_order, "USD", "100.00")

    with pytest.raises(database.psycopg.Error, match="больше доступной"):     # автоматический уже на всю сумму
        _run("SELECT request_refund(%s, 1, 'ещё')", (payment_id,))

    # Человек решил вернуть частями: отменяет автоматический запрос и создаёт свои.
    _run("UPDATE refunds SET status = 'canceled' WHERE payment_id = %s", (payment_id,))
    _run("SELECT request_refund(%s, 6000, 'частичный возврат')", (payment_id,))
    _run("SELECT request_refund(%s, 4000, 'остаток')", (payment_id,))
    with pytest.raises(database.psycopg.Error, match="больше доступной"):
        _run("SELECT request_refund(%s, 1, 'лишнее')", (payment_id,))

    rows = _run("SELECT amount_minor, currency, status FROM refunds WHERE payment_id = %s ORDER BY id", (payment_id,))
    assert rows == [(10000, "USD", "canceled"), (6000, "USD", "requested"), (4000, "USD", "requested")]
    assert scalar("SELECT currency FROM orders WHERE id = %s", (order["id"],)) == "RUB"   # не от заказа
    audit = _run("SELECT details->>'currency' FROM audit_log WHERE payment_id = %s AND action = 'refund.requested'",
                 (payment_id,))
    assert audit == [("USD",), ("USD",)]


def test_every_refund_path_is_bound_to_received_money(service, yookassa, new_order):
    order, _, _, payment_id, _ = _paid_in(service, yookassa, new_order, "USD", "100.00")

    for sql, match in (
        ("INSERT INTO refunds (payment_id, order_id, amount_minor, currency, reason) "
         "VALUES (%s, %s, 100, 'RUB', 'в валюте заказа')", "валюте учтённой оплаты"),
        ("INSERT INTO refunds (payment_id, order_id, amount_minor, currency, reason) "
         "VALUES (%s, %s, 100, 'USD', 'сверх полученного')", "больше доступной"),
    ):
        with pytest.raises(database.psycopg.Error, match=match):
            _run(sql, (payment_id, order["id"]))
    with pytest.raises(database.psycopg.Error, match="неизменяемы"):
        _run("UPDATE refunds SET currency = 'RUB' WHERE payment_id = %s", (payment_id,))
    with pytest.raises(database.psycopg.Error, match="неизменяемы"):
        _run("UPDATE refunds SET amount_minor = 1 WHERE payment_id = %s", (payment_id,))
    _run("UPDATE refunds SET status = 'canceled' WHERE payment_id = %s", (payment_id,))
    _run("SELECT request_refund(%s, 10000, 'заново')", (payment_id,))
    with pytest.raises(database.psycopg.Error, match="больше доступной"):     # «воскресить» отменённый нельзя
        _run("UPDATE refunds SET status = 'requested' WHERE payment_id = %s AND status = 'canceled'", (payment_id,))


def test_refund_requires_recorded_money(service, yookassa, new_order):
    order, token, _ = new_order()
    service.checkout(order["id"], token)
    payment_id = payment_row(order["id"])[0][0]
    with pytest.raises(database.psycopg.Error, match="не учтены"):
        _run("SELECT request_refund(%s, 100, 'x')", (payment_id,))
    with pytest.raises(database.psycopg.Error, match="не учтены"):
        _run("INSERT INTO refunds (payment_id, order_id, amount_minor, currency, reason) "
             "VALUES (%s, %s, 100, 'RUB', 'x')", (payment_id, order["id"]))


def test_unknown_currency_is_still_never_recorded(service, yookassa, new_order):
    order, _, _, payment_id, results = _paid_in(service, yookassa, new_order, "XXX", "100.00")
    assert results == ["retry_later"]
    assert scalar("SELECT count(*) FROM money_operations WHERE payment_id = %s", (payment_id,)) == 0
