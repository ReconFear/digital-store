"""Платёжный процесс целиком: настоящий адаптер ЮKassa -> локальный имитатор по HTTP,
настоящая тестовая база PostgreSQL (закоммиченные данные, несколько соединений).

Номера в комментариях — пункты обязательного списка тестов PR 3.
"""

import dataclasses
import json
import threading
import uuid

import pytest

import database
from payment_testkit import (  # noqa: F401  (фикстуры pytest)
    SECRET,
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
from payments.config import PaymentSettings
from payments.provider import ProviderUnavailable
from payments.service import PaymentService, PaymentServiceError
from payments.yookassa import YooKassaProvider


def _stock(pid):
    return scalar("SELECT quantity FROM products WHERE id = %s", (pid,))


def _order_status(order_id):
    return scalar("SELECT status FROM orders WHERE id = %s", (order_id,))


def _money(order_id):
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT kind, amount_minor, currency, provider_test FROM money_operations "
                           "WHERE order_id = %s ORDER BY id", (order_id,))
            return cursor.fetchall()


def _refunds(order_id):
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT amount_minor, currency, status, reason FROM refunds WHERE order_id = %s ORDER BY id",
                           (order_id,))
            return cursor.fetchall()


def _fulfillments(order_id):
    return scalar("SELECT count(*) FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id "
                  "WHERE i.order_id = %s", (order_id,))


def _checkout_and_pay(service, yookassa, order, token):
    result = service.checkout(order["id"], token)
    (_, status, provider_id, *_rest) = payment_row(order["id"])[-1]
    assert status == "pending"
    yookassa.set_status(provider_id, "succeeded")
    return provider_id, result


# ---------- 1. настоящий запрос с точной суммой; 200 на создание != оплата ----------

def test_checkout_sends_exact_order_amount_and_does_not_mark_paid(service, yookassa, new_order):
    order, token, pid = new_order(price_minor=12345, quantity=2)

    result = service.checkout(order["id"], token)

    method, path, headers, body = yookassa.created_payments()[0]
    sent = json.loads(body)
    assert path == "/v3/payments"
    assert sent["amount"] == {"value": "246.90", "currency": "RUB"}   # 12345 * 2 копеек
    assert sent["capture"] is True
    assert sent["metadata"] == {"order_id": str(order["id"]), "payment_attempt": str(payment_row(order["id"])[0][0])}
    assert sent["confirmation"] == {"type": "redirect", "return_url": "https://shop.example/return"}
    assert str(uuid.UUID(headers["idempotence-key"])) == headers["idempotence-key"]
    assert headers["authorization"].startswith("Basic ")
    assert payment_row(order["id"])[0][1:5] == ("pending", payment_row(order["id"])[0][2], 24690, "RUB")
    assert result["payment_status"] == "awaiting_payment"
    assert result["confirmation_url"].startswith("https://yoomoney.mock/")
    # Ответ 200 на создание платежа — не оплата.
    assert _order_status(order["id"]) == "pending"
    assert _stock(pid) == 10
    assert _money(order["id"]) == []


def test_minor_units_are_converted_exactly_without_float(service, yookassa, new_order):
    order, token, _ = new_order(price_minor=1, quantity=3)  # 0.03 RUB: 0.1 + 0.2 не участвует
    service.checkout(order["id"], token)
    assert json.loads(yookassa.created_payments()[0][3])["amount"]["value"] == "0.03"


def test_repeated_checkout_reuses_the_same_payment(service, yookassa, new_order):
    order, token, _ = new_order()
    first = service.checkout(order["id"], token)
    second = service.checkout(order["id"], token)

    assert first == second
    assert len(yookassa.created_payments()) == 1
    assert len(payment_row(order["id"])) == 1


def test_full_payment_via_notification_and_worker(service, yookassa, new_order):
    order, token, pid = new_order(price_minor=5000, quantity=2)
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)

    service.accept_notification(notification("payment.succeeded", provider_id), "185.71.76.1")
    assert _order_status(order["id"]) == "pending"          # приём уведомления сам ничего не проводит
    results = service.process_due_events()

    assert [r[1] for r in results] == ["succeeded"]
    assert _order_status(order["id"]) == "paid"
    assert _stock(pid) == 8
    assert _money(order["id"]) == [("payment", 10000, "RUB", True)]
    assert payment_row(order["id"])[0][1] == "succeeded"
    assert _fulfillments(order["id"]) == 1
    status = service.order_status(order["id"], token)
    assert (status["order"]["status"], status["payment_status"], status["fulfillment_status"],
            status["refund_status"]) == ("paid", "succeeded", "pending", "none")


# ---------- 2. подмена суммы или валюты ----------

def test_provider_amount_different_from_order_is_not_accepted_as_payment(service, yookassa, new_order):
    order, token, pid = new_order(price_minor=10000)
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    yookassa.tamper(provider_id, amount="1.00")   # провайдер говорит: оплачено 1 рубль

    assert service.sync_provider_payment(provider_id) == "requires_action"

    assert _order_status(order["id"]) == "pending"
    assert _stock(pid) == 10
    assert payment_row(order["id"])[0][1] == "requires_action"
    assert payment_row(order["id"])[0][5] == "amount_mismatch"
    # Деньги честно учтены в той сумме, что получена, и ждут возврата.
    assert _money(order["id"]) == [("payment", 100, "RUB", True)]
    assert _refunds(order["id"]) == [(100, "RUB", "requested", "amount_mismatch")]


def test_unsupported_currency_from_provider_never_pays_order(service, yookassa, new_order):
    order, token, pid = new_order()
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    # rev3: USD и другие известные валюты ISO теперь учитываются как currency_mismatch
    # (test_payments_rev3.py); неизвестная валюта по-прежнему — ошибка протокола, учёта нет.
    yookassa.tamper(provider_id, currency="XXX")
    service.accept_notification(notification("payment.succeeded", provider_id), "x")

    assert [r[1] for r in service.process_due_events()] == ["retry_later"]
    assert _order_status(order["id"]) == "pending"
    assert payment_row(order["id"])[0][1] == "pending"
    assert scalar("SELECT last_error FROM provider_events WHERE object_id = %s", (provider_id,)) == \
        "ProviderProtocolError"


def test_currency_mismatch_in_verified_state_goes_to_requires_action(service, yookassa, new_order):
    # Валюта из списка поддерживаемых, но не та, что у заказа: проверяет сама база.
    order, token, _ = new_order()
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    payment_id = payment_row(order["id"])[0][0]
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT payment_record_provider_state(%s, %s, 'succeeded', %s, 'EUR', TRUE, TRUE, NULL)",
                           (payment_id, provider_id, order["total"]["amount_minor"]))
            result = cursor.fetchone()[0]
    result = json.loads(result) if isinstance(result, str) else result
    assert result["reason"] == "currency_mismatch"
    assert _order_status(order["id"]) == "pending"


def test_database_refuses_payment_attempt_with_amount_other_than_order_total(new_order):
    order, _, _ = new_order(price_minor=10000)
    with pytest.raises(Exception, match="сумма платежа должна быть равна итогу заказа"):
        with database.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO payments (order_id, provider, idempotence_key, amount_minor, currency) "
                    "VALUES (%s, 'yookassa', gen_random_uuid(), 1, 'RUB')", (order["id"],))


def test_payment_amount_and_order_cannot_be_rewritten(service, new_order):
    order, token, _ = new_order(price_minor=10000)
    service.checkout(order["id"], token)
    for sql in ("UPDATE payments SET amount_minor = 1 WHERE order_id = %s",
                "UPDATE payments SET currency = 'USD' WHERE order_id = %s",
                "UPDATE payments SET provider_payment_id = 'other' WHERE order_id = %s",
                "UPDATE payments SET status = 'creating' WHERE order_id = %s",
                "UPDATE payments SET status = 'succeeded' WHERE order_id = %s"):   # без денежной операции
        with pytest.raises(database.psycopg.Error):
            with database.get_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql, (order["id"],))
    assert payment_row(order["id"])[0][3] == 10000


# ---------- 3. поддельные уведомления и ложный succeeded ----------

def test_fake_notification_for_unknown_payment_changes_nothing(service, yookassa, new_order):
    order, token, pid = new_order()
    service.checkout(order["id"], token)

    service.accept_notification(notification("payment.succeeded", "fake-" + uuid.uuid4().hex[:20]), "x")
    results = service.process_due_events()

    assert [r[1] for r in results] == ["not_found_at_provider"]
    assert _order_status(order["id"]) == "pending"
    assert _stock(pid) == 10


def test_false_succeeded_for_real_pending_payment_is_rechecked(service, yookassa, new_order):
    order, token, pid = new_order()
    service.checkout(order["id"], token)
    provider_id = payment_row(order["id"])[0][2]

    # Тело «уведомления» утверждает succeeded; у провайдера платёж по-прежнему pending.
    service.accept_notification(notification("payment.succeeded", provider_id), "x")
    assert [r[1] for r in service.process_due_events()] == ["not_final"]

    assert _order_status(order["id"]) == "pending"
    assert payment_row(order["id"])[0][1] == "pending"
    assert _money(order["id"]) == []


def test_foreign_succeeded_payment_is_ignored(service, yookassa, new_order):
    order, token, _ = new_order()
    service.checkout(order["id"], token)
    foreign = yookassa.add_foreign_payment(metadata={"order_id": str(order["id"])})

    service.accept_notification(notification("payment.succeeded", foreign), "x")

    assert [r[1] for r in service.process_due_events()] == ["unknown_payment"]
    assert _order_status(order["id"]) == "pending"


def test_payment_of_another_shop_is_not_accepted(service, yookassa, new_order):
    order, token, pid = new_order()
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    yookassa.tamper(provider_id, account_id="999999")

    assert service.sync_provider_payment(provider_id) == "requires_action"
    assert payment_row(order["id"])[0][5] == "shop_mismatch"
    assert _order_status(order["id"]) == "pending"
    assert _money(order["id"]) == []


def test_metadata_pointing_to_another_order_is_not_accepted(service, yookassa, new_order):
    order, token, _ = new_order()
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    yookassa.tamper(provider_id, metadata={"order_id": "1", "payment_attempt": "1"})

    assert service.sync_provider_payment(provider_id) == "requires_action"
    assert payment_row(order["id"])[0][5] == "metadata_mismatch"
    assert _order_status(order["id"]) == "pending"


def test_live_payment_in_test_mode_and_vice_versa_is_not_accepted(service, yookassa, new_order):
    order, token, _ = new_order()
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    yookassa.tamper(provider_id, test=False)

    assert service.sync_provider_payment(provider_id) == "requires_action"
    assert payment_row(order["id"])[0][5] == "test_mode_mismatch"
    assert _order_status(order["id"]) == "pending"


@pytest.mark.parametrize("body", [
    b"", b"not json", b"[]", b'{"type": "notification"}',
    b'{"type": "notification", "event": "payment.succeeded", "object": {"id": "../../etc"}}',
    b'{"type": "other", "event": "payment.succeeded", "object": {"id": "abc"}}',
])
def test_malformed_notifications_are_rejected_not_faked(service, body):
    with pytest.raises(PaymentServiceError) as error:
        service.accept_notification(body, "x")
    assert error.value.http_status == 400


def test_notification_sender_ip_is_checked(provider, pay_settings, clean_events):
    strict = PaymentService(provider, dataclasses.replace(pay_settings, webhook_ip_check=True))
    with pytest.raises(PaymentServiceError) as error:
        strict.accept_notification(notification("payment.succeeded", "abc"), "203.0.113.5")
    assert error.value.http_status == 403
    assert strict.accept_notification(notification("payment.succeeded", "abc-ip"), "185.71.76.10") > 0
    assert strict.accept_notification(notification("payment.succeeded", "abc-ip6"), "2a02:5180::1") > 0


def test_notifications_are_refused_when_payments_disabled(provider):
    disabled = PaymentService(provider, PaymentSettings())
    with pytest.raises(PaymentServiceError) as error:
        disabled.accept_notification(notification("payment.succeeded", "abc"), "185.71.76.10")
    assert error.value.http_status == 404


# ---------- 4. два параллельных подтверждения ----------

def test_parallel_confirmations_record_payment_once(service, yookassa, new_order):
    order, token, pid = new_order(stock=5, quantity=2)
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    barrier = threading.Barrier(6)
    results, errors = [], []

    def confirm():
        try:
            barrier.wait()
            results.append(service.sync_provider_payment(provider_id))
        except Exception as exc:  # noqa: BLE001 — собрать и проверить ниже
            errors.append(exc)

    threads = [threading.Thread(target=confirm) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == []
    assert sorted(results) == ["already_final"] * 5 + ["succeeded"]
    assert _stock(pid) == 3
    assert len(_money(order["id"])) == 1
    assert _fulfillments(order["id"]) == 1


# ---------- 5. повторная доставка ----------

def test_repeated_delivery_is_stored_once_and_applied_once(service, yookassa, new_order):
    order, token, pid = new_order()
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)

    for _ in range(3):
        service.accept_notification(notification("payment.succeeded", provider_id), "x")
    assert scalar("SELECT deliveries FROM provider_events WHERE object_id = %s", (provider_id,)) == 3
    assert len(service.process_due_events()) == 1

    service.accept_notification(notification("payment.succeeded", provider_id), "x")   # уже обработано
    assert service.process_due_events() == []
    assert scalar("SELECT status FROM provider_events WHERE object_id = %s", (provider_id,)) == "processed"
    assert _stock(pid) == 9
    assert len(_money(order["id"])) == 1


# ---------- 6. события не по порядку ----------

def test_late_events_after_success_do_not_change_anything(service, yookassa, new_order):
    order, token, pid = new_order()
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    service.accept_notification(notification("payment.succeeded", provider_id), "x")
    service.process_due_events()

    for event in ("payment.waiting_for_capture", "payment.canceled"):
        service.accept_notification(notification(event, provider_id), "x")
    assert sorted(r[1] for r in service.process_due_events()) == ["already_final", "already_final"]
    assert payment_row(order["id"])[0][1] == "succeeded"
    assert _order_status(order["id"]) == "paid"
    assert _stock(pid) == 9


def test_success_notification_before_local_binding_is_matched_by_provider_metadata(
        service, yookassa, new_order, monkeypatch):
    # Платёж создан у провайдера, но ответ до нас не дошёл (ID не сохранён), а уведомление пришло.
    order, token, pid = new_order()
    yookassa.fail_next("500_after_processing", count=3, method="POST")
    with pytest.raises(PaymentServiceError):
        service.checkout(order["id"], token)
    assert payment_row(order["id"])[0][1:3] == ("unknown", None)
    provider_id = next(iter(yookassa.payments))
    yookassa.set_status(provider_id, "succeeded")

    service.accept_notification(notification("payment.succeeded", provider_id), "x")
    assert [r[1] for r in service.process_due_events()] == ["succeeded"]
    assert payment_row(order["id"])[0][1:3] == ("succeeded", provider_id)
    assert _order_status(order["id"]) == "paid"
    assert len(yookassa.payments) == 1


def test_canceled_payment_allows_new_attempt(service, yookassa, new_order):
    order, token, pid = new_order()
    service.checkout(order["id"], token)
    first = payment_row(order["id"])[0][2]
    yookassa.set_status(first, "canceled")
    service.accept_notification(notification("payment.canceled", first), "x")
    service.process_due_events()
    assert payment_row(order["id"])[0][1] == "canceled"
    assert _order_status(order["id"]) == "pending"

    service.checkout(order["id"], token)
    rows = payment_row(order["id"])
    assert [r[1] for r in rows] == ["canceled", "pending"]
    assert rows[0][6] != rows[1][6]   # новая попытка — новый ключ идемпотентности


# ---------- 7. таймауты провайдера и повтор ----------

def test_timeouts_leave_attempt_unknown_and_retry_reuses_key(service, yookassa, new_order):
    order, token, _ = new_order()
    yookassa.fail_next("timeout", count=3, method="POST")

    with pytest.raises(PaymentServiceError) as error:
        service.checkout(order["id"], token)
    assert error.value.http_status == 503
    assert payment_row(order["id"])[0][1] == "unknown"
    keys = {r[2]["idempotence-key"] for r in yookassa.created_payments()}
    assert len(keys) == 1                      # все повторы — с одним ключом

    result = service.checkout(order["id"], token)
    assert result["confirmation_url"]
    assert {r[2]["idempotence-key"] for r in yookassa.created_payments()} == keys
    assert len(yookassa.payments) == 1
    assert payment_row(order["id"])[0][1] == "pending"


def test_lost_responses_never_create_second_payment(service, yookassa, new_order):
    order, token, _ = new_order()
    yookassa.fail_next("500_after_processing", count=3, method="POST")   # платёж создан, ответы потеряны
    with pytest.raises(PaymentServiceError):
        service.checkout(order["id"], token)

    service.checkout(order["id"], token)
    assert len(yookassa.payments) == 1
    assert payment_row(order["id"])[0][2] == next(iter(yookassa.payments))


def test_adapter_retries_transient_failures_within_one_call(service, yookassa, new_order):
    order, token, _ = new_order()
    yookassa.fail_next("drop", method="POST")
    yookassa.fail_next("429", method="POST")
    result = service.checkout(order["id"], token)

    assert result["confirmation_url"]
    assert len(yookassa.created_payments()) == 3
    assert len(yookassa.payments) == 1


def test_provider_rejection_marks_attempt_failed_and_allows_new_one(service, yookassa, new_order):
    order, token, _ = new_order()
    yookassa.fail_next("400", method="POST")
    with pytest.raises(PaymentServiceError):
        service.checkout(order["id"], token)
    assert payment_row(order["id"])[0][1] == "failed"
    service.checkout(order["id"], token)
    assert [r[1] for r in payment_row(order["id"])] == ["failed", "pending"]


def test_worker_retries_event_when_provider_is_unavailable(service, yookassa, new_order):
    order, token, _ = new_order()
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    service.accept_notification(notification("payment.succeeded", provider_id), "x")
    yookassa.fail_next("500", count=3, method="GET")

    assert [r[1] for r in service.process_due_events()] == ["retry_later"]
    assert _order_status(order["id"]) == "pending"
    assert scalar("SELECT status FROM provider_events WHERE object_id = %s", (provider_id,)) == "failed"

    with database.get_connection() as conn:   # «прошло время» — событие снова к обработке
        with conn.cursor() as cursor:
            cursor.execute("UPDATE provider_events SET next_attempt_at = now() WHERE object_id = %s", (provider_id,))
    assert [r[1] for r in service.process_due_events()] == ["succeeded"]
    assert _order_status(order["id"]) == "paid"


# ---------- 8. ошибка базы после успешного ответа провайдера ----------

class _FailingBind:
    """Соединения как обычно, но вызов payment_attempt_bind падает (сбой БД до COMMIT)."""

    def __init__(self):
        self.armed = True

    def __call__(self):
        return _ConnProxy(database.get_connection(), self)


class _ConnProxy:
    def __init__(self, conn, switch):
        self._conn, self._switch = conn, switch

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)

    def cursor(self):
        return _CursorProxy(self._conn.cursor(), self._switch)


class _CursorProxy:
    def __init__(self, cursor, switch):
        self._cursor, self._switch = cursor, switch

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self._cursor.__exit__(*exc)

    def execute(self, query, params=None):
        if ("payment_attempt_bind" in query or "payment_checkout_bind" in query) and self._switch.armed:
            self._switch.armed = False
            raise database.psycopg.OperationalError("имитация: соединение с БД потеряно")
        return self._cursor.execute(query, params)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()


def test_db_failure_after_provider_success_is_recovered_without_second_payment(
        provider, pay_settings, yookassa, new_order, clean_events):
    order, token, _ = new_order()
    failing = PaymentService(provider, pay_settings, connect=_FailingBind())

    with pytest.raises(database.psycopg.OperationalError):
        failing.checkout(order["id"], token)
    assert payment_row(order["id"])[0][1:3] == ("creating", None)
    assert len(yookassa.payments) == 1         # у провайдера платёж уже есть

    # Сверка (после «перезапуска») повторяет запрос с тем же ключом и привязывает ТОТ ЖЕ платёж.
    recovered = PaymentService(provider, pay_settings)
    recovered.stale_attempt_grace_seconds = 0
    assert recovered.reconcile()["recreated"] == 1
    assert payment_row(order["id"])[0][1:3] == ("pending", next(iter(yookassa.payments)))
    assert len(yookassa.payments) == 1


def test_db_failure_after_provider_success_is_recovered_by_next_checkout(
        provider, pay_settings, yookassa, new_order, clean_events):
    order, token, _ = new_order()
    with pytest.raises(database.psycopg.OperationalError):
        PaymentService(provider, pay_settings, connect=_FailingBind()).checkout(order["id"], token)

    result = PaymentService(provider, pay_settings).checkout(order["id"], token)
    assert result["confirmation_url"] == yookassa.payments[next(iter(yookassa.payments))]["confirmation"][
        "confirmation_url"]
    assert len(yookassa.payments) == 1


# ---------- 9. конкурентная оплата при ограниченном остатке ----------

def test_concurrent_paid_orders_with_limited_stock(service, yookassa, new_order, make_product):
    pid = make_product(price_minor=1000, quantity=1)
    from payments.access import create_order_with_access
    orders = [create_order_with_access([(pid, 1)]) for _ in range(3)]
    provider_ids = []
    for order, token in orders:
        provider_ids.append(_checkout_and_pay(service, yookassa, order, token)[0])

    barrier = threading.Barrier(len(provider_ids))
    errors = []

    def confirm(provider_id):
        try:
            barrier.wait()
            service.sync_provider_payment(provider_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=confirm, args=(p,)) for p in provider_ids]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == []
    statuses = sorted(_order_status(o["id"]) for o, _ in orders)
    assert statuses == ["paid", "requires_action", "requires_action"]
    assert _stock(pid) == 0                                   # не отрицательный, списан один раз
    assert all(len(_money(o["id"])) == 1 for o, _ in orders)   # все деньги учтены


# ---------- 10. деньги приняты, товара не хватает ----------

def test_money_received_but_stock_short_then_admin_resolves(service, yookassa, new_order):
    order, token, pid = new_order(stock=2, quantity=2)
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    with database.get_connection() as conn:   # товар продан в другом канале
        with conn.cursor() as cursor:
            cursor.execute("UPDATE products SET quantity = 1 WHERE id = %s", (pid,))

    assert service.sync_provider_payment(provider_id) == "succeeded"

    assert payment_row(order["id"])[0][1] == "succeeded"          # платёж честно учтён
    assert _money(order["id"]) == [("payment", order["total"]["amount_minor"], "RUB", True)]
    assert _order_status(order["id"]) == "requires_action"         # заказ ждёт решения
    assert _stock(pid) == 1 and _fulfillments(order["id"]) == 0
    assert service.order_status(order["id"], token)["payment_status"] == "succeeded"

    # Повторное уведомление ничего не списывает.
    assert service.sync_provider_payment(provider_id) == "already_final"
    assert _stock(pid) == 1

    # Администратор пополнил склад и провёл заказ.
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE products SET quantity = 5 WHERE id = %s", (pid,))
            cursor.execute("SELECT admin_retry_paid_order(%s)", (order["id"],))
    assert _order_status(order["id"]) == "paid"
    assert _stock(pid) == 3 and _fulfillments(order["id"]) == 1


def test_admin_retry_requires_recorded_payment(new_order):
    order, _, _ = new_order()
    with pytest.raises(Exception, match="нет учтённой успешной оплаты"):
        with database.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT admin_retry_paid_order(%s)", (order["id"],))
    assert _order_status(order["id"]) == "pending"


def test_payment_for_cancelled_order_is_recorded_with_refund_request(service, yookassa, new_order):
    order, token, pid = new_order()
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE orders SET status = 'cancelled' WHERE id = %s", (order["id"],))

    assert service.sync_provider_payment(provider_id) == "succeeded"
    assert _order_status(order["id"]) == "cancelled"
    assert _stock(pid) == 10
    assert _refunds(order["id"]) == [(order["total"]["amount_minor"], "RUB", "requested", "order_not_payable")]


def test_refund_request_cannot_exceed_received_money(service, yookassa, new_order):
    order, token, _ = new_order(price_minor=1000)
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    service.sync_provider_payment(provider_id)
    payment_id = payment_row(order["id"])[0][0]
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT request_refund(%s, 600, 'покупатель отказался')", (payment_id,))
    with pytest.raises(Exception, match="больше доступной"):
        with database.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT request_refund(%s, 500, 'ещё')", (payment_id,))
    assert _refunds(order["id"]) == [(600, "RUB", "requested", "покупатель отказался")]
    assert service.order_status(order["id"], token)["refund_status"] == "pending"


# ---------- 12. чужой заказ ----------

def test_order_cannot_be_read_or_paid_without_its_secret(service, yookassa, new_order):
    mine, my_token, _ = new_order()
    other, other_token, _ = new_order()

    for order_id, token in ((other["id"], my_token), (mine["id"], other_token), (mine["id"], "x" * 43),
                            (mine["id"], ""), (mine["id"], None), (mine["id"] + 10_000_000, my_token),
                            (-1, my_token), (2 ** 40, my_token)):
        for call in (service.order_status, service.checkout):
            with pytest.raises(PaymentServiceError) as error:
                call(order_id, token)
            assert (error.value.http_status, error.value.code) == (404, "order_not_found")
    assert payment_row(mine["id"]) == [] and payment_row(other["id"]) == []
    assert service.order_status(mine["id"], my_token)["order"]["id"] == mine["id"]


def test_token_is_stored_only_as_hash(new_order):
    order, token, _ = new_order()
    stored = scalar("SELECT encode(token_sha256, 'hex') FROM order_access WHERE order_id = %s", (order["id"],))
    import hashlib
    assert stored == hashlib.sha256(token.encode()).hexdigest()
    assert token not in stored


def test_v1_orders_have_no_token_and_cannot_be_paid(service, make_product):
    pid = make_product(price_minor=50000)
    order = database.create_order_in_db(pid)
    assert scalar("SELECT count(*) FROM order_access WHERE order_id = %s", (order["id"],)) == 0
    with pytest.raises(PaymentServiceError):
        service.checkout(order["id"], "A" * 43)


def test_paid_order_is_not_payable_again(service, yookassa, new_order):
    order, token, _ = new_order()
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    service.sync_provider_payment(provider_id)
    with pytest.raises(PaymentServiceError) as error:
        service.checkout(order["id"], token)
    assert error.value.http_status == 409
    assert len(yookassa.payments) == 1


def test_status_does_not_expose_internal_payment_details(service, yookassa, new_order):
    order, token, _ = new_order()
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    yookassa.tamper(provider_id, account_id="999999")
    service.sync_provider_payment(provider_id)

    status = service.order_status(order["id"], token)
    text = json.dumps(status)
    assert status["payment_status"] == "requires_action"
    for secret in ("shop_mismatch", provider_id, "999999", SECRET, token, "idempotence"):
        assert secret not in text


def test_payments_disabled_by_default(provider, new_order):
    order, token, _ = new_order()
    with pytest.raises(PaymentServiceError) as error:
        PaymentService(provider, PaymentSettings()).checkout(order["id"], token)
    assert error.value.http_status == 503


# ---------- 17. восстановление после перезапуска ----------

def test_event_accepted_before_restart_is_processed_by_new_worker(provider, pay_settings, yookassa, new_order,
                                                                  clean_events):
    order, token, pid = new_order()
    web = PaymentService(provider, pay_settings)
    provider_id, _ = _checkout_and_pay(web, yookassa, order, token)
    web.accept_notification(notification("payment.succeeded", provider_id), "x")
    del web                                                     # «процесс завершился»

    fresh_provider = YooKassaProvider.from_settings(pay_settings, sleep=lambda s: None)
    worker = PaymentService(fresh_provider, pay_settings)
    assert worker.reconcile()["events"] == 1
    assert _order_status(order["id"]) == "paid"


def test_event_claimed_by_crashed_worker_is_retried_after_lease(service, yookassa, new_order, monkeypatch):
    order, token, _ = new_order()
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    service.accept_notification(notification("payment.succeeded", provider_id), "x")
    service.event_lease_seconds = 0

    def crash(_):
        raise SystemExit("обработчик упал после захвата события")

    monkeypatch.setattr(service, "sync_provider_payment", crash)
    with pytest.raises(SystemExit):
        service.process_due_events()
    monkeypatch.undo()
    assert scalar("SELECT attempts FROM provider_events WHERE object_id = %s", (provider_id,)) == 1

    assert [r[1] for r in service.process_due_events()] == ["succeeded"]
    assert _order_status(order["id"]) == "paid"


def test_reconcile_finds_paid_payment_without_any_notification(service, yookassa, new_order):
    order, token, pid = new_order()
    _checkout_and_pay(service, yookassa, order, token)   # уведомление так и не пришло

    report = service.reconcile()
    assert report["checked"] == 1
    assert _order_status(order["id"]) == "paid"
    assert service.reconcile()["checked"] == 0           # конечный платёж больше не проверяется


def test_concurrent_checkouts_create_one_attempt_and_one_payment(service, yookassa, new_order):
    order, token, _ = new_order()
    barrier = threading.Barrier(5)
    results, errors = [], []

    def go():
        try:
            barrier.wait()
            results.append(service.checkout(order["id"], token)["confirmation_url"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]

    assert errors == []
    assert len(set(results)) == 1
    assert len(payment_row(order["id"])) == 1
    assert len(yookassa.payments) == 1


def test_unavailable_provider_error_is_not_swallowed_by_adapter(provider, yookassa):
    yookassa.fail_next("500", count=3, method="GET")
    with pytest.raises(ProviderUnavailable):
        provider.get_payment("abc")


# ---------- 14. логи и секреты ----------

def test_payment_flow_logs_contain_no_secrets(service, yookassa, new_order, caplog):
    import base64
    import logging
    caplog.set_level(logging.DEBUG)
    order, token, _ = new_order()
    yookassa.fail_next("timeout", method="POST")
    provider_id, _ = _checkout_and_pay(service, yookassa, order, token)
    service.accept_notification(notification("payment.succeeded", provider_id), "x")
    yookassa.fail_next("500", count=3, method="GET")
    service.process_due_events()
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE provider_events SET next_attempt_at = now() WHERE object_id = %s", (provider_id,))
    service.process_due_events()

    text = caplog.text
    assert "payment.succeeded" in text                       # журнал действительно пишется
    for secret in (SECRET, token, base64.b64encode(f"123456:{SECRET}".encode()).decode(),
                   payment_row(order["id"])[0][6]):           # секрет магазина, секрет заказа, ключ идемпотентности
        assert secret not in text
    stored = scalar("SELECT string_agg(coalesce(last_error, ''), ' ') FROM provider_events")
    assert SECRET not in stored


# Части имени столбца, означающие данные карт, секреты провайдера или пароли. Проверяется КАЖДАЯ часть имени
# (card_number, provider_secret_key, user_password), а не только начало.
FORBIDDEN_COLUMN_PARTS = {"card", "pan", "cvv", "cvv2", "cvc", "cvc2", "secret", "password", "passwd", "pwd"}
# PR6 (миграция 0005): единственные разрешённые столбцы со словом password — в таблице покупателей, и только
# при подтверждённом контексте (см. _assert_customer_password_columns_are_hash_only).
ALLOWED_PASSWORD_COLUMNS = {("customers", "password_hash"), ("customers", "password_changed_at")}


def test_database_stores_no_card_data_or_provider_secrets():
    columns = {
        (table, column)
        for table, column in _rows("SELECT table_name, column_name FROM information_schema.columns "
                                   "WHERE table_schema = 'public'")
    }
    offenders = sorted(f"{table}.{column}" for table, column in columns
                       if FORBIDDEN_COLUMN_PARTS & set(column.split("_"))
                       and (table, column) not in ALLOWED_PASSWORD_COLUMNS)
    assert offenders == [], offenders
    if columns & ALLOWED_PASSWORD_COLUMNS:
        _assert_customer_password_columns_are_hash_only()


def _assert_customer_password_columns_are_hash_only():
    """Исключение для PR6 — только при контексте, исключающем открытый пароль: password_hash — NOT NULL text
    с CHECK на формат bcrypt ($2a$, стоимость, 53 символа соли и хэша) и с триггером единой стоимости;
    password_changed_at — только отметка времени. Открытый пароль в базу не передаётся вовсе: функции
    принимают HMAC-доказательство, хэшируемое bcrypt (ext_pgcrypto.crypt)."""
    types = dict(_rows("SELECT column_name, data_type || ':' || is_nullable FROM information_schema.columns "
                       "WHERE table_schema = 'public' AND table_name = 'customers' "
                       "AND column_name IN ('password_hash', 'password_changed_at')"))
    assert types == {"password_hash": "text:NO", "password_changed_at": "timestamp with time zone:NO"}, types
    checks = [definition for (definition,) in _rows(
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c WHERE c.contype = 'c' "
        "AND c.conrelid = 'public.customers'::regclass AND c.conname = 'customers_password_hash_check'")]
    assert len(checks) == 1, checks
    assert "password_hash ~" in checks[0] and "2a" in checks[0] and "{53}" in checks[0], checks[0]
    triggers = [name for (name,) in _rows(
        "SELECT tgname FROM pg_trigger WHERE tgrelid = 'public.customers'::regclass AND NOT tgisinternal "
        "AND tgenabled <> 'D'")]
    assert "customers_hash_cost_guard" in triggers, triggers


def _rows(sql):
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchall()
