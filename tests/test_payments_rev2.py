"""PR 3 rev2: аренда событий, «потерянные» попытки старше срока ключа, проверка ответа на
создание платежа. Настоящий адаптер ЮKassa -> локальный имитатор по HTTP, настоящая
тестовая база PostgreSQL, несколько соединений и потоков.
"""

import json
import threading

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
from payments.access import token_sha256_hex
from payments.provider import ProviderUnavailable
from payments.service import PaymentService, PaymentServiceError, _Attempt, confirmation_url_allowed


def _run(sql, params=None):
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall() if cursor.description else None


def _event(object_id):
    rows = _run("SELECT status, deliveries, attempts, lease_token::text, recheck_requested, last_error, "
                "next_attempt_at, processed_at FROM provider_events WHERE object_id = %s", (object_id,))
    return dict(zip(("status", "deliveries", "attempts", "lease", "recheck", "last_error", "next_at",
                     "processed_at"), rows[0], strict=True))


def _paid_and_notified(service, yookassa, new_order, **order):
    order, token, pid = new_order(**order)
    service.checkout(order["id"], token)
    provider_id = payment_row(order["id"])[0][2]
    yookassa.set_status(provider_id, "succeeded")
    service.accept_notification(notification("payment.succeeded", provider_id), "x")
    return order, token, pid, provider_id


class _Blocking:
    """Обёртка провайдера: get_payment останавливается, пока тест не отпустит его."""

    def __init__(self, inner, fail_after_release=False):
        self.inner = inner
        self.name = inner.name
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail_after_release = fail_after_release

    def get_payment(self, provider_payment_id):
        self.entered.set()
        assert self.release.wait(10), "тест не отпустил обработчик"
        if self.fail_after_release:
            raise ProviderUnavailable("имитация: провайдер недоступен")
        return self.inner.get_payment(provider_payment_id)

    def create_payment(self, **kwargs):
        return self.inner.create_payment(**kwargs)

    def find_payment_by_metadata(self, **kwargs):
        return self.inner.find_payment_by_metadata(**kwargs)


def _start_worker(worker):
    box = {}

    def run():
        try:
            box["result"] = worker.process_due_events()
        except Exception as exc:  # noqa: BLE001 — проверяется в тесте
            box["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    return thread, box


# ================= 1. аренда событий =================

def test_leased_event_is_not_taken_by_second_worker_and_redelivery_keeps_the_lease(
        service, provider, pay_settings, yookassa, new_order):
    order, token, pid, provider_id = _paid_and_notified(service, yookassa, new_order)
    blocking = _Blocking(provider)
    worker_a = PaymentService(blocking, pay_settings)
    worker_b = PaymentService(provider, pay_settings)

    thread, box = _start_worker(worker_a)
    assert blocking.entered.wait(10)
    leased = _event(provider_id)
    assert leased["status"] == "processing" and leased["lease"]

    # Повторный webhook прямо во время обработки: аренда и срок не трогаются.
    service.accept_notification(notification("payment.succeeded", provider_id), "x")
    during = _event(provider_id)
    assert (during["status"], during["lease"], during["next_at"]) == ("processing", leased["lease"], leased["next_at"])
    assert during["deliveries"] == 2 and during["recheck"] is True

    assert worker_b.process_due_events() == []        # второй обработчик событие не берёт

    blocking.release.set()
    thread.join(10)
    # Доставка во время обработки -> после завершения событие вернулось в очередь, и тот же
    # обработчик (он ещё в цикле) перепроверил его у провайдера: второй захват, already_final.
    assert "error" not in box and [r[1] for r in box["result"]] == ["succeeded", "already_final"]
    after = _event(provider_id)
    assert (after["status"], after["lease"], after["recheck"], after["attempts"]) == ("processed", None, False, 2)
    assert worker_b.process_due_events() == []
    assert scalar("SELECT quantity FROM products WHERE id = %s", (pid,)) == 9
    assert scalar("SELECT count(*) FROM money_operations WHERE order_id = %s", (order["id"],)) == 1


@pytest.mark.parametrize("stale_outcome", ["success", "provider_error"])
def test_expired_lease_is_reclaimed_and_stale_worker_cannot_overwrite(
        service, provider, pay_settings, yookassa, new_order, stale_outcome):
    order, _, pid, provider_id = _paid_and_notified(service, yookassa, new_order)
    blocking = _Blocking(provider, fail_after_release=(stale_outcome == "provider_error"))
    worker_a = PaymentService(blocking, pay_settings)
    worker_a.event_lease_seconds = 0                    # аренда A истекает сразу
    worker_b = PaymentService(provider, pay_settings)

    thread, box = _start_worker(worker_a)
    assert blocking.entered.wait(10)
    lease_a = _event(provider_id)["lease"]

    assert [r[1] for r in worker_b.process_due_events()] == ["succeeded"]   # B перехватил истёкшую аренду
    done_by_b = _event(provider_id)
    assert done_by_b["status"] == "processed" and done_by_b["attempts"] == 2

    blocking.release.set()                               # «зависший» A просыпается
    thread.join(10)
    assert "error" not in box and [r[1] for r in box["result"]] == ["stale_lease"]
    final = _event(provider_id)
    assert final == done_by_b                            # результат B не изменён
    assert final["lease"] is None and lease_a is not None
    assert scalar("SELECT quantity FROM products WHERE id = %s", (pid,)) == 9
    assert scalar("SELECT count(*) FROM money_operations WHERE order_id = %s", (order["id"],)) == 1


def test_crashed_worker_lease_blocks_until_expiry_then_event_is_retried(service, yookassa, new_order, monkeypatch):
    order, _, _, provider_id = _paid_and_notified(service, yookassa, new_order)

    def crash(_):
        raise SystemExit("обработчик упал после захвата события")

    monkeypatch.setattr(service, "sync_provider_payment", crash)
    with pytest.raises(SystemExit):
        service.process_due_events()
    monkeypatch.undo()

    assert _event(provider_id)["status"] == "processing"
    assert service.process_due_events() == []            # аренда (5 минут) ещё действует
    _run("UPDATE provider_events SET lease_expires_at = now() - interval '1 second' WHERE object_id = %s",
         (provider_id,))                                  # «прошло 5 минут»
    assert [r[1] for r in service.process_due_events()] == ["succeeded"]
    assert _event(provider_id)["attempts"] == 2
    assert scalar("SELECT status FROM orders WHERE id = %s", (order["id"],)) == "paid"


def test_failed_event_backoff_is_not_reset_by_redelivery(service, yookassa, new_order):
    _, _, _, provider_id = _paid_and_notified(service, yookassa, new_order)
    yookassa.fail_next("500", count=3, method="GET")
    assert [r[1] for r in service.process_due_events()] == ["retry_later"]
    failed = _event(provider_id)
    service.accept_notification(notification("payment.succeeded", provider_id), "x")
    assert _event(provider_id)["next_at"] == failed["next_at"]
    assert service.process_due_events() == []


def test_owner_of_expired_but_untaken_lease_can_still_finish(service, yookassa, new_order):
    _, _, _, provider_id = _paid_and_notified(service, yookassa, new_order)
    service.event_lease_seconds = 0
    assert [r[1] for r in service.process_due_events()] == ["succeeded"]
    assert _event(provider_id)["status"] == "processed"


def test_lease_columns_are_consistent_by_constraint(service, yookassa, new_order):
    _, _, _, provider_id = _paid_and_notified(service, yookassa, new_order)
    with pytest.raises(database.psycopg.Error):
        _run("UPDATE provider_events SET status = 'processing' WHERE object_id = %s", (provider_id,))
    with pytest.raises(database.psycopg.Error):
        _run("UPDATE provider_events SET lease_token = gen_random_uuid() WHERE object_id = %s", (provider_id,))


# ================= 2. результат создания неизвестен дольше срока ключа =================

def _lose_attempt(service, yookassa, order, token, mode):
    """Все попытки создания с неизвестным результатом; затем «прошло 2 суток»."""
    yookassa.fail_next(mode, count=3, method="POST")
    with pytest.raises(PaymentServiceError):
        service.checkout(order["id"], token)
    assert payment_row(order["id"])[0][1] == "unknown"
    with database.get_connection() as conn:   # одна транзакция: триггер выключен только внутри неё
        with conn.cursor() as cursor:
            cursor.execute("ALTER TABLE payments DISABLE TRIGGER payments_guard")
            cursor.execute("UPDATE payments SET created_at = now() - interval '2 days' WHERE order_id = %s",
                           (order["id"],))
            cursor.execute("ALTER TABLE payments ENABLE TRIGGER payments_guard")
    for payment in yookassa.payments.values():   # у «провайдера» платёж тоже двухдневной давности
        ts = scalar("SELECT to_char((now() - interval '2 days' + interval '1 minute') AT TIME ZONE 'UTC', "
                    "'YYYY-MM-DD\"T\"HH24:MI:SS.MS\"Z\"')")
        payment["created_at"] = ts
    return payment_row(order["id"])[0][0]


def test_unknown_attempt_older_than_key_blocks_new_checkout_and_late_success_pays_once(
        service, yookassa, new_order):
    order, token, pid = new_order()
    attempt_id = _lose_attempt(service, yookassa, order, token, "500_after_processing")   # платёж создан
    posts_before = len(yookassa.created_payments())
    assert len(yookassa.payments) == 1

    for _ in range(2):   # покупатель жмёт «Оплатить» снова — новой ссылки и нового платежа нет
        with pytest.raises(PaymentServiceError) as error:
            service.checkout(order["id"], token)
        assert (error.value.http_status, error.value.code) == (409, "payment_under_review")
    assert payment_row(order["id"])[0][1:6:4] == ("requires_action", "unresolved_attempt")
    assert len(payment_row(order["id"])) == 1
    assert len(yookassa.created_payments()) == posts_before and len(yookassa.payments) == 1
    assert service.order_status(order["id"], token)["payment_status"] == "requires_action"

    # Покупатель оплатил «потерянный» платёж. Сверка находит его в списке платежей ЮKassa.
    lost = next(iter(yookassa.payments))
    yookassa.set_status(lost, "succeeded")
    service.unresolved_recheck_seconds = 0
    report = service.reconcile()
    assert report["discovered"] == 1

    assert payment_row(order["id"])[0][1:3] == ("succeeded", lost)
    assert scalar("SELECT status FROM orders WHERE id = %s", (order["id"],)) == "paid"
    assert scalar("SELECT quantity FROM products WHERE id = %s", (pid,)) == 9
    assert scalar("SELECT count(*) FROM money_operations WHERE order_id = %s", (order["id"],)) == 1
    assert scalar("SELECT count(*) FROM refunds WHERE order_id = %s", (order["id"],)) == 0
    with pytest.raises(PaymentServiceError) as error:
        service.checkout(order["id"], token)
    assert error.value.code == "order_not_payable"
    assert len(yookassa.payments) == 1                    # вторая оплата так и не стала возможной
    actions = [r[0] for r in _run("SELECT action FROM audit_log WHERE payment_id = %s ORDER BY id", (attempt_id,))]
    assert "payment.attempt_requires_action" in actions and "payment.unresolved_found" in actions


def test_unresolved_attempt_found_by_late_notification(service, yookassa, new_order):
    order, token, pid = new_order()
    _lose_attempt(service, yookassa, order, token, "500_after_processing")
    with pytest.raises(PaymentServiceError):              # checkout сам переводит в unresolved_attempt
        service.checkout(order["id"], token)
    assert payment_row(order["id"])[0][5] == "unresolved_attempt"

    lost = next(iter(yookassa.payments))
    yookassa.set_status(lost, "succeeded")
    service.accept_notification(notification("payment.succeeded", lost), "x")
    assert [r[1] for r in service.process_due_events()] == ["succeeded"]
    assert scalar("SELECT status FROM orders WHERE id = %s", (order["id"],)) == "paid"


def test_unresolved_attempt_not_found_needs_admin_and_old_money_is_still_recorded(service, yookassa, new_order):
    order, token, _ = new_order()
    attempt_id = _lose_attempt(service, yookassa, order, token, "timeout")   # до провайдера не дошло
    assert yookassa.payments == {}
    service.stale_attempt_grace_seconds = 0
    service.unresolved_recheck_seconds = 0
    report = service.reconcile()
    assert (report["unresolved"], report["discovered"]) == (1, 0)
    assert payment_row(order["id"])[0][1] == "requires_action"
    with pytest.raises(PaymentServiceError) as error:
        service.checkout(order["id"], token)
    assert error.value.code == "payment_under_review"     # «не нашли в списке» ещё не доказательство

    # Администратор проверил кабинет ЮKassa и подтвердил: денег по попытке нет.
    _run("SELECT admin_resolve_payment(%s, 'not_paid_confirmed', 'проверено в кабинете ЮKassa')", (attempt_id,))
    assert service.order_status(order["id"], token)["payment_status"] == "canceled"
    service.checkout(order["id"], token)
    assert [r[1] for r in payment_row(order["id"])] == ["requires_action", "pending"]

    # Если по старой попытке всё же придут деньги — они учитываются, нужен возврат, решение снято.
    late = yookassa.add_foreign_payment(metadata={"order_id": str(order["id"]), "payment_attempt": str(attempt_id)})
    service.accept_notification(notification("payment.succeeded", late), "x")
    assert [r[1] for r in service.process_due_events()] == ["late_success_recorded"]
    row = _run("SELECT status, status_reason, resolution FROM payments WHERE id = %s", (attempt_id,))[0]
    assert row == ("requires_action", "late_payment", None)
    assert _run("SELECT reason, status FROM refunds WHERE payment_id = %s", (attempt_id,)) == \
        [("payment_after_final_state", "requested")]
    assert scalar("SELECT status FROM orders WHERE id = %s", (order["id"],)) == "pending"
    # Повторное уведомление ничего не добавляет.
    service.accept_notification(notification("payment.waiting_for_capture", late), "x")
    assert [r[1] for r in service.process_due_events()] == ["already_final"]
    assert scalar("SELECT count(*) FROM money_operations WHERE payment_id = %s", (attempt_id,)) == 1


def test_incomplete_provider_search_draws_no_conclusion(service, yookassa, new_order):
    order, token, _ = new_order()
    _lose_attempt(service, yookassa, order, token, "500_after_processing")
    service.stale_attempt_grace_seconds = 0
    service.unresolved_recheck_seconds = 0
    yookassa.fail_next("500", count=10, method="GET")
    report = service.reconcile()
    assert report["discovered"] == 0
    assert payment_row(order["id"])[0][1] == "requires_action"
    assert _run("SELECT last_checked_at FROM payments WHERE order_id = %s", (order["id"],))[0][0] is None


def test_admin_cannot_declare_not_paid_when_money_was_recorded(service, yookassa, new_order):
    order, token, _ = new_order(price_minor=10000)
    service.checkout(order["id"], token)
    provider_id = payment_row(order["id"])[0][2]
    yookassa.set_status(provider_id, "succeeded")
    yookassa.tamper(provider_id, amount="1.00")
    service.sync_provider_payment(provider_id)   # amount_mismatch: деньги учтены
    payment_id = payment_row(order["id"])[0][0]
    with pytest.raises(database.psycopg.Error, match="учтены деньги"):
        _run("SELECT admin_resolve_payment(%s, 'not_paid_confirmed', 'x')", (payment_id,))
    with pytest.raises(PaymentServiceError) as error:
        service.checkout(order["id"], token)
    assert error.value.code == "payment_under_review"
    _run("SELECT admin_resolve_payment(%s, 'handled_manually', 'возврат 1 ₽ сделан в кабинете')", (payment_id,))
    assert service.checkout(order["id"], token)["confirmation_url"]


# ================= 3. проверка ответа на создание платежа =================

@pytest.mark.parametrize("override, reason", [
    ({"recipient": {"account_id": "999999", "gateway_id": "1"}}, "shop"),
    ({"test": False}, "test_mode"),
    ({"paid": True}, "status_paid"),
    ({"status": "succeeded"}, "status_paid"),                       # succeeded, но paid = false
    ({"status": "canceled", "paid": True}, "status_paid"),
    ({"amount": {"value": "1.00", "currency": "RUB"}}, "amount"),
    ({"metadata": {"order_id": "1", "payment_attempt": "1"}}, "metadata"),
    ({"confirmation": {"type": "redirect", "confirmation_url": "http://yoomoney.mock/pay"}}, "confirmation_url"),
    ({"confirmation": {"type": "redirect", "confirmation_url": "https://evil.example/pay"}}, "confirmation_url"),
    ({"confirmation": {"type": "redirect", "confirmation_url": "javascript:alert(1)"}}, "confirmation_url"),
    ({"confirmation": {"type": "redirect", "confirmation_url": "https://u:p@yoomoney.mock/pay"}}, "confirmation_url"),
    ({"confirmation": {"type": "redirect", "confirmation_url": "https://yoomoney.mock.evil.com/"}}, "confirmation_url"),
    ({"confirmation": None}, "confirmation_url"),
])
def test_inconsistent_create_response_gives_no_link(service, yookassa, new_order, override, reason):
    order, token, _ = new_order()
    yookassa.create_overrides = override

    with pytest.raises(PaymentServiceError) as error:
        service.checkout(order["id"], token)

    assert error.value.http_status == 503
    row = payment_row(order["id"])[0]
    assert (row[1], row[2], row[5]) == ("requires_action", None, f"create_response_mismatch:{reason}")
    assert _run("SELECT confirmation_url FROM payments WHERE order_id = %s", (order["id"],)) == [(None,)]
    status = service.order_status(order["id"], token)
    assert status["confirmation_url"] is None and status["payment_status"] == "requires_action"
    yookassa.create_overrides = {}
    with pytest.raises(PaymentServiceError) as again:    # и новая попытка не создаётся
        service.checkout(order["id"], token)
    assert again.value.code == "payment_under_review"


def test_consistent_non_pending_create_response_gives_no_link(service, yookassa, new_order):
    order, token, _ = new_order()
    yookassa.create_overrides = {"status": "waiting_for_capture", "paid": True}
    result = service.checkout(order["id"], token)
    assert result == {"payment_status": "awaiting_payment", "confirmation_url": None}


def test_provider_returning_another_payment_id_for_bound_attempt_gives_no_link(service, yookassa, new_order):
    order, token, _ = new_order()
    service.checkout(order["id"], token)
    row = payment_row(order["id"])[0]
    # Тот же ключ идемпотентности, но «провайдер» отвечает другим платежом.
    _, pid_bound = row[0], row[2]
    other = yookassa.add_foreign_payment(status="succeeded")
    yookassa.payments[other]["status"] = "pending"
    yookassa.payments[other]["paid"] = False
    yookassa.payments[other]["confirmation"] = {"type": "redirect", "confirmation_url": "https://yoomoney.mock/o"}
    yookassa.payments[other]["metadata"] = {"order_id": str(order["id"]), "payment_attempt": str(row[0])}
    yookassa.payments[other]["amount"] = {"value": f"{order['total']['amount_minor'] / 100:.2f}", "currency": "RUB"}
    key = row[6]
    request_body = yookassa.idempotence[key][0]
    yookassa.idempotence[key] = (request_body, other)

    attempt = _Attempt(row[0], order["id"], key, row[3], row[4], "creating", None, False)
    with pytest.raises(PaymentServiceError):
        service._create_and_bind(attempt, token_sha256_hex(token))
    assert payment_row(order["id"])[0][2] == pid_bound
    assert scalar("SELECT count(*) FROM audit_log WHERE payment_id = %s AND action = 'payment.bind_id_conflict'",
                  (row[0],)) == 1


def test_worker_rejects_inconsistent_provider_state(service, yookassa, new_order):
    order, token, _ = new_order()
    service.checkout(order["id"], token)
    provider_id = payment_row(order["id"])[0][2]
    yookassa.tamper(provider_id, status="succeeded", paid=False)
    assert service.sync_provider_payment(provider_id) == "requires_action"
    assert payment_row(order["id"])[0][5] == "provider_state_inconsistent"
    assert scalar("SELECT status FROM orders WHERE id = %s", (order["id"],)) == "pending"


@pytest.mark.parametrize("url, allowed", [
    ("https://yoomoney.ru/checkout/payments/v2/contract?orderId=1", True),
    ("https://sub.yoomoney.ru/x", True),
    ("https://yoomoney.ru:443/x", True),
    ("https://yoomoney.ru:8443/x", False),
    ("http://yoomoney.ru/x", False),
    ("https://yoomoney.ru.evil.com/x", False),
    ("https://evilyoomoney.ru/x", False),
    ("https://user@yoomoney.ru/x", False),
    ("https://yoomoney.ru/x y", False),
    ("https://yoomoney.ru/" + "a" * 2100, False),
    ("", False), (None, False), ("//yoomoney.ru/x", False),
])
def test_confirmation_url_policy(url, allowed):
    assert confirmation_url_allowed(url, ("yoomoney.ru", "yookassa.ru")) is allowed


def test_search_in_provider_list_pages_through_results(provider, yookassa):
    for _ in range(205):
        yookassa.add_foreign_payment(metadata={"order_id": "9", "payment_attempt": "1"})
    target = yookassa.add_foreign_payment(status="succeeded", metadata={"order_id": "7", "payment_attempt": "42"})
    yookassa.payments[target]["created_at"] = "2000-01-01T00:00:00.000Z"   # самый старый — на последней странице
    found = provider.find_payment_by_metadata(payment_attempt=42, order_id=7, created_gte="1999-12-31T00:00:00.000Z",
                                              created_lt="2999-01-01T00:00:00.000Z")
    assert found.id == target
    pages = [r for r in yookassa.requests if r[0] == "GET" and "created_at.gte" in r[1]]
    assert len(pages) == 3 and all("limit=100" in r[1] for r in pages)
    assert provider.find_payment_by_metadata(payment_attempt=43, order_id=7, created_gte="1999-12-31T00:00:00.000Z",
                                             created_lt="2999-01-01T00:00:00.000Z") is None
    assert provider.find_payment_by_metadata(payment_attempt=42, order_id=7, created_gte="2001-01-01T00:00:00.000Z",
                                             created_lt="2999-01-01T00:00:00.000Z") is None


def test_list_answer_must_be_a_list(provider, yookassa):
    from payments.provider import ProviderProtocolError
    yookassa.fail_next("garbage", method="GET")
    with pytest.raises(ProviderProtocolError):
        provider.find_payment_by_metadata(payment_attempt=1, order_id=1, created_gte="2000-01-01T00:00:00.000Z",
                                          created_lt="2999-01-01T00:00:00.000Z")


def test_order_status_payload_is_unchanged_in_shape(service, yookassa, new_order):
    order, token, _ = new_order(quantity=2)
    status = service.order_status(order["id"], token)
    assert set(status) == {"order", "payment_status", "confirmation_url", "fulfillment_status", "refund_status"}
    assert status["order"]["items"] == order["items"] and status["order"]["total"] == order["total"]
    assert json.dumps(status)


def test_stale_worker_cannot_finish_while_new_lease_holder_is_still_working(
        service, provider, pay_settings, yookassa, new_order):
    order, _, pid, provider_id = _paid_and_notified(service, yookassa, new_order)
    block_a, block_b = _Blocking(provider), _Blocking(provider)
    worker_a = PaymentService(block_a, pay_settings)
    worker_a.event_lease_seconds = 0
    worker_b = PaymentService(block_b, pay_settings)

    thread_a, box_a = _start_worker(worker_a)
    assert block_a.entered.wait(10)
    thread_b, box_b = _start_worker(worker_b)            # B перехватывает истёкшую аренду A
    assert block_b.entered.wait(10)
    lease_b = _event(provider_id)["lease"]

    block_a.release.set()                                 # A заканчивает, пока B ещё работает
    thread_a.join(10)
    # Учёт оплаты A выполнил (идемпотентно), но записать итог события не смог — аренда чужая.
    assert [r[1] for r in box_a["result"]] == ["stale_lease"]
    during = _event(provider_id)
    assert (during["status"], during["lease"]) == ("processing", lease_b)   # аренда B не тронута

    block_b.release.set()
    thread_b.join(10)
    assert "error" not in box_b
    assert _event(provider_id)["status"] == "processed"
    assert scalar("SELECT quantity FROM products WHERE id = %s", (pid,)) == 9
    assert scalar("SELECT count(*) FROM money_operations WHERE order_id = %s", (order["id"],)) == 1
