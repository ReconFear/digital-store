"""PR 3 rev4: восстановление ссылки при повторной сверке привязанного «потерянного» платежа
и запрет автоматических HTTP-перенаправлений в адаптере ЮKassa.

Настоящий адаптер -> локальный имитатор ЮKassa по HTTP (не настоящая ЮKassa),
настоящая тестовая база PostgreSQL.
"""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import database
from payment_testkit import (  # noqa: F401  (фикстуры pytest)
    SECRET,
    SHOP_ID,
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
from payments.provider import ProviderUnavailable
from payments.service import PaymentService, PaymentServiceError
from test_payments_rev2 import _lose_attempt, _run

AUTH = "Basic " + base64.b64encode(f"{SHOP_ID}:{SECRET}".encode()).decode()


def _row(order_id):
    return _run("SELECT status, status_reason, provider_payment_id, confirmation_url FROM payments "
                "WHERE order_id = %s ORDER BY id", (order_id,))


def _sweep(service):
    service.stale_attempt_grace_seconds = 0
    service.unresolved_recheck_seconds = 0
    service.open_payment_recheck_seconds = 0
    return service.reconcile()


# ================= 1. ссылка появилась при повторной сверке =================

def _lost_and_found_without_link(service, yookassa, new_order, **order_kw):
    order, token, pid = new_order(**order_kw)
    _lose_attempt(service, yookassa, order, token, "500_after_processing")
    lost = next(iter(yookassa.payments))
    link = yookassa.payments[lost].pop("confirmation")["confirmation_url"]   # в списке ЮKassa — без ссылки
    _sweep(service)
    assert _row(order["id"]) == [("requires_action", "unresolved_attempt", lost, None)]
    with pytest.raises(PaymentServiceError) as error:
        service.checkout(order["id"], token)
    assert error.value.code == "payment_under_review"
    return order, token, pid, lost, link


def test_link_appearing_on_later_get_resumes_the_same_payment_and_stock_is_taken_once(service, yookassa, new_order):
    order, token, pid, lost, link = _lost_and_found_without_link(service, yookassa, new_order,
                                                                 price_minor=30000, quantity=2, stock=4)
    posts = len(yookassa.created_payments())

    # Через 5 минут GET /payments/{id} возвращает тот же pending, но уже со ссылкой.
    yookassa.payments[lost]["confirmation"] = {"type": "redirect", "confirmation_url": link}
    _sweep(service)

    assert _row(order["id"]) == [("pending", None, lost, link)]
    result = service.checkout(order["id"], token)
    assert result == {"payment_status": "awaiting_payment", "confirmation_url": link}
    status = service.order_status(order["id"], token)
    assert (status["payment_status"], status["confirmation_url"]) == ("awaiting_payment", link)
    assert len(yookassa.payments) == 1 and len(yookassa.created_payments()) == posts   # новый платёж не создан
    assert scalar("SELECT count(*) FROM audit_log WHERE order_id = %s AND action = 'payment.unresolved_resumed'",
                  (order["id"],)) == 1

    yookassa.set_status(lost, "succeeded")
    service.accept_notification(notification("payment.succeeded", lost), "x")
    assert [r[1] for r in service.process_due_events()] == ["succeeded"]
    service.accept_notification(notification("payment.succeeded", lost), "x")      # повтор
    _sweep(service)

    assert _row(order["id"])[0][:3] == ("succeeded", None, lost)
    assert scalar("SELECT status FROM orders WHERE id = %s", (order["id"],)) == "paid"
    assert scalar("SELECT quantity FROM products WHERE id = %s", (pid,)) == 2          # 4 - 2, один раз
    assert _run("SELECT amount_minor, currency FROM money_operations WHERE order_id = %s",
                (order["id"],)) == [(60000, "RUB")]
    assert scalar("SELECT count(*) FROM refunds WHERE order_id = %s", (order["id"],)) == 0


@pytest.mark.parametrize("bad_link", ["http://yoomoney.mock/pay", "https://evil.example/pay",
                                      "https://u:p@yoomoney.mock/pay", None])
def test_bad_link_on_later_get_keeps_requires_action_and_409(service, yookassa, new_order, bad_link):
    order, token, _, lost, _ = _lost_and_found_without_link(service, yookassa, new_order)
    if bad_link:
        yookassa.payments[lost]["confirmation"] = {"type": "redirect", "confirmation_url": bad_link}
    _sweep(service)

    assert _row(order["id"]) == [("requires_action", "unresolved_attempt", lost, None)]
    with pytest.raises(PaymentServiceError) as error:
        service.checkout(order["id"], token)
    assert (error.value.http_status, error.value.code) == (409, "payment_under_review")
    status = service.order_status(order["id"], token)
    assert (status["payment_status"], status["confirmation_url"]) == ("requires_action", None)
    assert len(yookassa.payments) == 1


@pytest.mark.parametrize("change", [{"amount": "1.00"}, {"account_id": "999999"}, {"test": False}, {"paid": True}])
def test_link_from_later_get_is_not_stored_when_payment_does_not_match(service, yookassa, new_order, change):
    order, token, _, lost, link = _lost_and_found_without_link(service, yookassa, new_order)
    yookassa.payments[lost]["confirmation"] = {"type": "redirect", "confirmation_url": link}
    yookassa.tamper(lost, **change)
    _sweep(service)

    status, _, provider_id, stored = _row(order["id"])[0]
    assert (status, provider_id, stored) == ("requires_action", lost, None)
    with pytest.raises(PaymentServiceError):
        service.checkout(order["id"], token)


def test_parallel_resweep_resumes_once(service, provider, pay_settings, yookassa, new_order):
    order, token, _, lost, link = _lost_and_found_without_link(service, yookassa, new_order)
    yookassa.payments[lost]["confirmation"] = {"type": "redirect", "confirmation_url": link}
    barrier = threading.Barrier(4)
    errors = []

    def sync():
        try:
            barrier.wait()
            PaymentService(provider, pay_settings).sync_provider_payment(lost)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=sync) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    assert _row(order["id"]) == [("pending", None, lost, link)]
    assert scalar("SELECT count(*) FROM audit_log WHERE order_id = %s AND action = 'payment.unresolved_resumed'",
                  (order["id"],)) == 1
    assert service.checkout(order["id"], token)["confirmation_url"] == link


def test_database_refuses_to_resume_lost_attempt_without_https_link(service, yookassa, new_order):
    order, _, _, lost, _ = _lost_and_found_without_link(service, yookassa, new_order)
    payment_id = payment_row(order["id"])[0][0]
    for url in (None, "http://yoomoney.mock/pay", "javascript:alert(1)", "https://"):
        value = _run("SELECT payment_record_provider_state(%s, %s, 'pending', NULL, NULL, TRUE, TRUE, NULL, %s)",
                     (payment_id, lost, url))[0][0]
        value = json.loads(value) if isinstance(value, str) else value
        assert value["reason"] == "no_valid_confirmation_url"
    assert _row(order["id"])[0][0] == "requires_action"
    with pytest.raises(database.psycopg.Error, match="без https-ссылки"):      # даже владельцу таблиц
        _run("UPDATE payments SET status = 'pending' WHERE id = %s", (payment_id,))


# ================= 2. перенаправления не выполняются =================

class _Capture:
    """Посторонний сервер: записывает всё, что к нему пришло."""

    def __init__(self):
        self.requests = []
        capture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _any(self):
                capture.requests.append((self.command, self.path, dict(self.headers)))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            do_GET = do_POST = do_PUT = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def capture():
    server = _Capture()
    yield server
    server.stop()


def _create(provider):
    return provider.create_payment(idempotence_key="7c0c6f1e-9d5a-4f5e-8f00-00000000abcd", amount_minor=10000,
                                   currency="RUB", description="Заказ №1", return_url="https://shop.example/r",
                                   metadata={"order_id": "1", "payment_attempt": "1"})


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("target", ["foreign", "same_api"])
def test_redirect_is_never_followed_and_secret_never_leaves(provider, yookassa, capture, caplog, code, method, target):
    yookassa.redirect_location = capture.url + "/steal" if target == "foreign" else yookassa.url + "/payments/x"
    yookassa.fail_next(f"redirect_{code}", method=method)

    with pytest.raises(ProviderUnavailable) as error:     # результат неизвестен, а не отказ
        _create(provider) if method == "POST" else provider.get_payment("pay-1")

    assert capture.requests == []                          # по Location не ходили
    assert len(yookassa.requests) == 1                     # и к API повторно не обращались
    text = str(error.value) + caplog.text
    assert SECRET not in text and AUTH not in text and "steal" not in text


def test_unpatched_urllib_would_leak_the_secret(yookassa, capture):
    """Контроль: стандартный opener (как было до rev4) переносит Authorization по Location."""
    import urllib.error
    import urllib.request
    yookassa.redirect_location = capture.url + "/steal"
    yookassa.fail_next("redirect_307", method="GET")
    request = urllib.request.Request(yookassa.url + "/payments/pay-1", headers={"Authorization": AUTH})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(request, timeout=2).read()
    except urllib.error.URLError:
        pass
    assert capture.requests and capture.requests[0][2].get("Authorization") == AUTH


@pytest.mark.parametrize("code", [302, 307])
def test_redirect_on_create_keeps_attempt_unknown_and_retry_reuses_key(service, yookassa, capture, new_order, code):
    order, token, _ = new_order()
    yookassa.redirect_location = capture.url + "/steal"
    yookassa.fail_next(f"redirect_after_processing_{code}", method="POST")   # платёж создан, ответ — 3xx

    with pytest.raises(PaymentServiceError) as error:
        service.checkout(order["id"], token)
    assert (error.value.http_status, error.value.code) == (503, "payment_provider_unavailable")
    assert payment_row(order["id"])[0][1:3] == ("unknown", None)       # не failed: новая попытка не открыта
    assert capture.requests == []
    keys = {r[2]["idempotence-key"] for r in yookassa.created_payments()}

    result = service.checkout(order["id"], token)                        # повтор с ТЕМ ЖЕ ключом
    assert {r[2]["idempotence-key"] for r in yookassa.created_payments()} == keys
    assert len(yookassa.payments) == 1 and len(payment_row(order["id"])) == 1
    assert result["confirmation_url"] == yookassa.payments[payment_row(order["id"])[0][2]]["confirmation"][
        "confirmation_url"]


def test_redirect_on_status_check_is_retried_later_without_following(service, yookassa, capture, new_order):
    order, token, _ = new_order()
    service.checkout(order["id"], token)
    provider_id = payment_row(order["id"])[0][2]
    yookassa.set_status(provider_id, "succeeded")
    service.accept_notification(notification("payment.succeeded", provider_id), "x")
    yookassa.redirect_location = capture.url + "/steal"
    yookassa.fail_next("redirect_301", method="GET")

    assert [r[1] for r in service.process_due_events()] == ["retry_later"]
    assert capture.requests == []
    assert scalar("SELECT status FROM orders WHERE id = %s", (order["id"],)) == "pending"
    _run("UPDATE provider_events SET next_attempt_at = now() WHERE object_id = %s", (provider_id,))
    assert [r[1] for r in service.process_due_events()] == ["succeeded"]
