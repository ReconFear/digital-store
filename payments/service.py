"""Платёжный процесс: checkout, уведомления, сверка.

Разделение обязанностей и прав (DB_ROLES.md):

* Веб-процесс (роль digital_store_app) — checkout(), order_status(), accept_notification().
  Он создаёт попытку, ходит к провайдеру за ссылкой оплаты, привязывает ID платежа и
  кладёт уведомления в долговременную очередь provider_events. Все действия с заказом —
  через функции БД, требующие секрет этого заказа. Учесть оплату он НЕ может.
* Платёжный обработчик (роль digital_store_worker, `python -m payments.worker`) —
  process_due_events(), reconcile(). Он сам запрашивает состояние платежа у провайдера
  и только по этому ответу вызывает payment_record_provider_state, которая в ОДНОЙ
  транзакции пишет денежную операцию, меняет статус платежа и проводит заказ.

Тело уведомления никогда не используется как доказательство оплаты: из него берутся
только тип события и ID объекта, всё остальное — из ответа провайдера.

Сбой между ответом провайдера и COMMIT: ключ идемпотентности сохраняется в базе ДО
запроса; повтор (следующий checkout или сверка) идёт с тем же ключом, и провайдер
возвращает тот же платёж. Ключ ЮKassa живёт 24 часа. Попытка без ответа дольше 23 часов
становится requires_action / unresolved_attempt: новых попыток для заказа не создаётся,
пока платёж не найден у провайдера (поиск по списку платежей) или пока администратор не
подтвердил, что денег по ней нет (admin_resolve_payment). Подробно — PAYMENTS.md.
"""

import ipaddress
import json
import logging
import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

import database
from commerce.money import money_json
from payments.access import token_sha256_hex
from payments.provider import (
    PROVIDER_ID_RE,
    ProviderAuthError,
    ProviderError,
    ProviderNotFound,
    ProviderProtocolError,
    ProviderRejected,
    ProviderUnavailable,
)

log = logging.getLogger("digital_store.payments")

PAYMENT_EVENTS = frozenset({"payment.succeeded", "payment.canceled", "payment.waiting_for_capture"})
EVENT_TYPE_RE = re.compile(r"^[a-z_]{1,32}\.[a-z_]{1,31}$")
MAX_NOTIFICATION_BYTES = 64 * 1024
EVENT_LEASE_SECONDS = 300
EVENT_MAX_BACKOFF_SECONDS = 3600
OPEN_PAYMENT_RECHECK_SECONDS = 300
STALE_ATTEMPT_GRACE_SECONDS = 60
UNRESOLVED_RECHECK_SECONDS = 3600
UNRESOLVED_SEARCH_DAYS = 35
MAX_ORDER_ID = 2_147_483_647

# Согласованность статуса и признака paid в ответе ЮKassa.
CONSISTENT_PAID = {"pending": False, "waiting_for_capture": True, "succeeded": True, "canceled": False}


class PaymentServiceError(Exception):
    """Ошибка для клиента API: код, HTTP-статус и безопасное сообщение (без деталей)."""

    def __init__(self, code, http_status, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.http_status = http_status
        self.message = message

    def as_detail(self):
        return {"code": self.code, "message": self.message}


NOT_FOUND = ("order_not_found", 404, "Заказ не найден.")
DISABLED = ("payments_disabled", 503, "Оплата временно недоступна.")
UNAVAILABLE = ("payment_provider_unavailable", 503, "Платёжная система не ответила. Повторите попытку позже.")
UNDER_REVIEW = ("payment_under_review", 409,
                "Предыдущая попытка оплаты проверяется магазином. Новая оплата пока невозможна.")


@dataclass
class _Attempt:
    payment_id: int
    order_id: int
    idempotence_key: str
    amount_minor: int
    currency: str
    status: str
    confirmation_url: str | None
    expired: bool


def _jsonb(value):
    return json.loads(value) if isinstance(value, str) else value


def public_payment_status(status, resolution=None):
    """Статус платёжной попытки для покупателя (без внутренних подробностей)."""
    if status is None:
        return "none"
    if status in ("creating", "unknown", "pending", "waiting_for_capture"):
        return "awaiting_payment"
    if status == "requires_action" and resolution == "not_paid_confirmed":
        return "canceled"   # магазин подтвердил, что денег по попытке нет: можно оплатить снова
    return {"succeeded": "succeeded", "canceled": "canceled", "failed": "failed"}.get(status, "requires_action")


def confirmation_url_allowed(url, hosts):
    """Ссылку оплаты можно отдать покупателю: https, без учётных данных, домен из списка."""
    if not isinstance(url, str) or not 0 < len(url) <= 2048 or any(c.isspace() or ord(c) < 32 for c in url):
        return False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return False
    return any(host == h or host.endswith("." + h) for h in hosts)


class PaymentService:
    def __init__(self, provider, settings, connect=None):
        self.provider = provider
        self.settings = settings
        self._connect_override = connect
        # Интервалы (секунды). Тесты уменьшают их, чтобы не ждать.
        self.event_lease_seconds = EVENT_LEASE_SECONDS
        self.stale_attempt_grace_seconds = STALE_ATTEMPT_GRACE_SECONDS
        self.open_payment_recheck_seconds = OPEN_PAYMENT_RECHECK_SECONDS
        self.unresolved_recheck_seconds = UNRESOLVED_RECHECK_SECONDS

    def _connect(self):
        return (self._connect_override or database.get_connection)()

    def _require_enabled(self):
        if not self.settings.enabled:
            raise PaymentServiceError(*DISABLED)

    def _scalar(self, sql, params):
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchone()[0]

    # ================= веб-процесс =================

    def checkout(self, order_id, access_token):
        """Начать или продолжить оплату заказа. -> {"payment_status", "confirmation_url"}."""
        self._require_enabled()
        token_hex = token_sha256_hex(access_token)
        if token_hex is None or type(order_id) is not int or not 0 < order_id <= MAX_ORDER_ID:
            raise PaymentServiceError(*NOT_FOUND)

        attempt = self._start_attempt(order_id, token_hex)
        if attempt.status in ("pending", "waiting_for_capture"):
            return {"payment_status": "awaiting_payment", "confirmation_url": attempt.confirmation_url}
        if attempt.expired:
            # Ключ идемпотентности больше нельзя повторять, а платёж по нему мог появиться.
            # Новую попытку НЕ создаём: попытка ждёт поиска у провайдера или решения человека.
            log.error("платёж %s: результат создания неизвестен дольше срока ключа — нужна проверка",
                      attempt.payment_id)
            self._mark(attempt.payment_id, "requires_action", "unresolved_attempt", token_hex)
            raise PaymentServiceError(*UNDER_REVIEW)
        return self._create_and_bind(attempt, token_hex)

    def _start_attempt(self, order_id, token_hex):
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT payment_id, idempotence_key::text, amount_minor, currency, status,
                           confirmation_url, now() - attempt_created_at > interval '23 hours', error
                    FROM payment_attempt_start(%s, decode(%s, 'hex'), %s, %s::uuid)
                    """,
                    (order_id, token_hex, self.provider.name, str(uuid.uuid4())),
                )
                row = cursor.fetchone()
        error = row[7]
        if error == "not_found":
            raise PaymentServiceError(*NOT_FOUND)
        if error == "not_payable":
            raise PaymentServiceError("order_not_payable", 409, "Заказ уже оплачен, отменён или ожидает проверки.")
        if error == "free_order":
            raise PaymentServiceError("order_not_payable", 409, "Заказ с нулевой суммой не оплачивается.")
        if error == "payment_under_review":
            raise PaymentServiceError(*UNDER_REVIEW)
        return _Attempt(row[0], order_id, row[1], row[2], row[3], row[4], row[5], bool(row[6]))

    def _create_and_bind(self, attempt, token_hex=None):
        """Запрос создания платежа с СОХРАНЁННЫМ ключом, проверка ответа, привязка к попытке.

        token_hex — секрет заказа (веб-процесс); без него — обработчик (сверка).
        Ссылка оплаты возвращается, только если ответ провайдера полностью согласован.
        """
        metadata = {"order_id": str(attempt.order_id), "payment_attempt": str(attempt.payment_id)}
        try:
            payment = self.provider.create_payment(
                idempotence_key=attempt.idempotence_key,
                amount_minor=attempt.amount_minor,
                currency=attempt.currency,
                description=f"Заказ №{attempt.order_id}",
                return_url=self.settings.return_url,
                metadata=metadata,
            )
        except ProviderUnavailable:
            log.warning("платёж %s: результат создания неизвестен, повтор с тем же ключом позже", attempt.payment_id)
            self._mark(attempt.payment_id, "unknown", "provider_unavailable", token_hex)
            raise PaymentServiceError(*UNAVAILABLE) from None
        except (ProviderProtocolError, ProviderNotFound):
            log.error("платёж %s: ответ провайдера не соответствует протоколу", attempt.payment_id)
            self._mark(attempt.payment_id, "unknown", "protocol_error", token_hex)
            raise PaymentServiceError(*UNAVAILABLE) from None
        except ProviderAuthError:
            log.error("платёж %s: провайдер не принял учётные данные магазина", attempt.payment_id)
            self._mark(attempt.payment_id, "failed", "provider_auth", token_hex)
            raise PaymentServiceError(*UNAVAILABLE) from None
        except ProviderRejected as error:
            log.error("платёж %s: провайдер отклонил создание, код %s", attempt.payment_id, error.code)
            self._mark(attempt.payment_id, "failed", f"rejected:{error.code}", token_hex)
            raise PaymentServiceError(*UNAVAILABLE) from None

        problem = self._create_response_problem(attempt, payment, metadata)
        if problem:
            # Провайдер ответил не тем, что мы просили, или противоречиво. Ссылку не выдаём,
            # попытку не привязываем; новые попытки заказа блокируются до решения человека.
            log.error("платёж %s: ответ на создание отклонён (%s)", attempt.payment_id, problem)
            self._mark(attempt.payment_id, "requires_action", f"create_response_mismatch:{problem}", token_hex)
            raise PaymentServiceError(*UNAVAILABLE)

        url = payment.confirmation_url if payment.status == "pending" else None
        if token_hex is not None:
            bound = self._scalar("SELECT payment_checkout_bind(%s, decode(%s, 'hex'), %s, %s, %s)",
                                 (attempt.payment_id, token_hex, payment.id, url, payment.test))
        else:
            bound = self._scalar("SELECT payment_attempt_bind(%s, %s, %s, %s)",
                                 (attempt.payment_id, payment.id, url, payment.test))
        if bound in ("id_conflict", "not_found"):
            log.error("платёж %s: привязка ответа провайдера не выполнена (%s)", attempt.payment_id, bound)
            raise PaymentServiceError(*UNAVAILABLE)
        if bound != "pending":
            url = None   # попытку уже продвинул обработчик — ссылка больше не нужна
        log.info("платёж %s создан у провайдера (заказ %s)", attempt.payment_id, attempt.order_id)
        # Даже если провайдер сразу ответил succeeded, учёт делает обработчик по своему запросу.
        return {"payment_status": "awaiting_payment", "confirmation_url": url}

    def _create_response_problem(self, attempt, payment, metadata):
        if payment.metadata.get("payment_attempt") != metadata["payment_attempt"] \
                or payment.metadata.get("order_id") != metadata["order_id"]:
            return "metadata"
        if payment.amount_minor != attempt.amount_minor or payment.currency != attempt.currency:
            return "amount"
        if payment.account_id != self.settings.shop_id:
            return "shop"
        if payment.test is not self.settings.expect_test_payments:
            return "test_mode"
        if CONSISTENT_PAID.get(payment.status) is not payment.paid:
            return "status_paid"
        if payment.status == "pending" and not confirmation_url_allowed(payment.confirmation_url,
                                                                        self.settings.confirmation_hosts):
            return "confirmation_url"
        return None

    def _mark(self, payment_id, status, reason, token_hex=None):
        if token_hex is not None:
            self._scalar("SELECT payment_checkout_mark(%s, decode(%s, 'hex'), %s, %s)",
                         (payment_id, token_hex, status, reason))
        else:
            self._scalar("SELECT payment_attempt_mark(%s, %s, %s)", (payment_id, status, reason))

    def order_status(self, order_id, access_token):
        """Состояние заказа для владельца секрета. Без персональных данных и внутренних причин."""
        token_hex = token_sha256_hex(access_token)
        if token_hex is None or type(order_id) is not int or not 0 < order_id <= MAX_ORDER_ID:
            raise PaymentServiceError(*NOT_FOUND)
        view = self._scalar("SELECT order_status_view(%s, decode(%s, 'hex'))", (order_id, token_hex))
        if view is None:
            raise PaymentServiceError(*NOT_FOUND)
        view = _jsonb(view)
        order, currency = view["order"], view["order"]["currency"]
        payment = view.get("payment") or {}
        return {
            "order": {
                "id": order["id"],
                "status": order["status"],
                "created_at": order["created_at"],
                "total": money_json(order["total_minor"], currency),
                "items": [
                    {
                        "product_id": item["product_id"],
                        "name": item["name"],
                        "product_type": item["product_type"],
                        "quantity": item["quantity"],
                        "unit_price": money_json(item["unit_price_minor"], currency),
                        "line_total": money_json(item["line_total_minor"], currency),
                    }
                    for item in view["items"]
                ],
            },
            "payment_status": public_payment_status(payment.get("status"), payment.get("resolution")),
            "confirmation_url": payment.get("confirmation_url") if payment.get("status") == "pending" else None,
            "fulfillment_status": _fulfillment_summary(view["fulfillments"]),
            "refund_status": _refund_summary(view["refunds"]),
        }

    def client_ip_allowed(self, ip):
        if not self.settings.webhook_ip_check:
            return True
        try:
            address = ipaddress.ip_address(ip)
        except (TypeError, ValueError):
            return False
        return any(address in ipaddress.ip_network(net) for net in self.settings.notification_networks)

    def accept_notification(self, body, client_ip):
        """Уведомление провайдера -> долговременная очередь. Ответ 200 только после COMMIT.

        Тело используется только для выбора, ЧТО перепроверить у провайдера (тип события и ID).
        """
        if not self.settings.enabled:
            raise PaymentServiceError("not_found", 404, "Not found")
        if not self.client_ip_allowed(client_ip):
            log.warning("уведомление отклонено: адрес отправителя не из списка провайдера")
            raise PaymentServiceError("forbidden", 403, "Forbidden")
        if len(body) > MAX_NOTIFICATION_BYTES:
            raise PaymentServiceError("payload_too_large", 413, "Payload too large")
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise PaymentServiceError("bad_notification", 400, "Bad notification") from None
        if not isinstance(data, dict):
            raise PaymentServiceError("bad_notification", 400, "Bad notification")
        event = data.get("event")
        obj = data.get("object")
        object_id = obj.get("id") if isinstance(obj, dict) else None
        if (data.get("type") != "notification" or not isinstance(event, str) or not EVENT_TYPE_RE.match(event)
                or not isinstance(object_id, str) or not PROVIDER_ID_RE.match(object_id)):
            raise PaymentServiceError("bad_notification", 400, "Bad notification")

        initial = "received" if event in PAYMENT_EVENTS else "ignored"
        event_id = self._scalar("SELECT provider_event_accept(%s, %s, %s, %s)",
                                (self.provider.name, event, object_id, initial))
        log.info("уведомление %s принято в очередь (событие %s)", event_id, event)
        return event_id

    # ================= платёжный обработчик =================

    def sync_provider_payment(self, provider_payment_id):
        """Запросить платёж у провайдера и применить ПРОВЕРЕННОЕ состояние. -> результат строкой."""
        payment = self.provider.get_payment(provider_payment_id)
        if payment.id != provider_payment_id:
            raise ProviderProtocolError("провайдер вернул другой платёж")
        return self._apply_provider_payment(payment)

    def _apply_provider_payment(self, payment):
        """Применить состояние платежа, полученное ОТ ПРОВАЙДЕРА (GET или поиск по списку)."""
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, order_id, amount_minor, currency, status, status_reason, resolved_at IS NULL
                    FROM payments WHERE provider = %s AND provider_payment_id = %s
                    """,
                    (self.provider.name, payment.id),
                )
                row = cursor.fetchone()
                needs_bind = row is None
                if needs_bind:
                    # Ответ на создание мог не дойти до нас — находим попытку по метаданным ПРОВАЙДЕРА.
                    attempt = payment.metadata.get("payment_attempt", "")
                    if not attempt.isdigit() or len(attempt) > 18:
                        return "unknown_payment"
                    cursor.execute(
                        """
                        SELECT id, order_id, amount_minor, currency, status, status_reason, resolved_at IS NULL
                        FROM payments
                        WHERE id = %s AND provider = %s AND provider_payment_id IS NULL
                          AND status IN ('creating', 'unknown', 'requires_action', 'failed')
                        """,
                        (int(attempt), self.provider.name),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        return "unknown_payment"
                payment_id, order_id, amount_minor, currency, local_status, reason, unresolved = row
                lost_attempt = local_status == "requires_action" and reason == "unresolved_attempt" and unresolved

                problem = self._provider_state_problem(payment, payment_id, order_id, amount_minor, currency)
                link_ok = (problem is None and payment.status == "pending"
                           and confirmation_url_allowed(payment.confirmation_url, self.settings.confirmation_hosts))

                if needs_bind:
                    # Ссылку передаём только после полной проверки; иначе NULL.
                    cursor.execute("SELECT payment_attempt_bind(%s, %s, %s, %s)",
                                   (payment_id, payment.id, payment.confirmation_url if link_ok else None,
                                    payment.test))
                    if cursor.fetchone()[0] == "id_conflict":
                        return "unknown_payment"

                if lost_attempt and problem is None and payment.status == "pending" and not link_ok:
                    # «Потерянный» платёж найден, ждёт оплаты, но пригодной ссылки нет: продолжить
                    # его покупатель не сможет. Честно оставляем requires_action (новые попытки
                    # заблокированы) и ждём, пока провайдер его отменит — тогда оплата откроется.
                    cursor.execute("SELECT payment_attempt_touch(%s, %s)::text",
                                   (payment_id, "found_pending_without_valid_link" if needs_bind else None))
                    log.warning("попытка %s: платёж найден у провайдера, но без пригодной ссылки оплаты", payment_id)
                    return "requires_action"

                if problem:
                    cursor.execute(
                        "SELECT payment_record_provider_state(%s, %s, 'requires_action', NULL, NULL, %s, %s, %s)",
                        (payment_id, payment.id, payment.test, self.settings.expect_test_payments, problem),
                    )
                else:
                    # Ссылка передаётся только проверенная (link_ok): «потерянная» попытка вернётся к
                    # оплате вместе с ней в той же транзакции — и при первой находке, и при повторной
                    # сверке уже привязанного платежа (rev4).
                    cursor.execute(
                        "SELECT payment_record_provider_state(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                        (payment_id, payment.id, payment.status, payment.amount_minor, payment.currency,
                         payment.test, self.settings.expect_test_payments, payment.cancellation_reason,
                         payment.confirmation_url if link_ok else None),
                    )
                result = _jsonb(cursor.fetchone()[0])
        log.info("платёж %s (заказ %s): у провайдера %s, результат %s", payment_id, order_id, payment.status,
                 result.get("result"))
        return result.get("result")

    def _provider_state_problem(self, payment, payment_id, order_id, amount_minor, currency):
        """Причина, по которой состояние провайдера нельзя применить как есть, или None.

        Для succeeded расхождение суммы, валюты и режима НЕ проверяется здесь: деньги уже
        получены, и payment_record_provider_state учтёт их как есть и создаст запрос возврата.
        Для остальных статусов (денег ещё нет) такой платёж просто нельзя продолжать.
        """
        if (payment.metadata.get("payment_attempt") != str(payment_id)
                or payment.metadata.get("order_id") != str(order_id)):
            return "metadata_mismatch"
        if payment.account_id != self.settings.shop_id:
            return "shop_mismatch"
        if CONSISTENT_PAID.get(payment.status) is not payment.paid:
            return "provider_state_inconsistent"
        if payment.status != "succeeded":
            if payment.amount_minor != amount_minor:
                return "amount_mismatch"
            if payment.currency != currency:
                return "currency_mismatch"
            if payment.test is not self.settings.expect_test_payments:
                return "test_mode_mismatch"
        return None

    # ---------- очередь уведомлений: аренда ----------

    def process_due_events(self, limit=50):
        """Обработать накопленные уведомления.

        Событие захватывается арендой: status = processing, новый lease_token, срок аренды.
        Завершение и ошибка записываются только при совпадении lease_token, поэтому
        обработчик, чья аренда истекла и перешла к другому, ничего не перезапишет.
        """
        results = []
        for _ in range(limit):
            claimed = self._claim_event()
            if claimed is None:
                break
            event_id, event_type, object_id, attempts, lease = claimed
            try:
                outcome = self.sync_provider_payment(object_id)
            except ProviderNotFound:
                results.append((event_id, self._finish_event(event_id, lease, "ignored", "not_found_at_provider")
                                or "not_found_at_provider"))
                continue
            except ProviderError as error:
                stale = self._fail_event(event_id, lease, attempts, type(error).__name__)
                results.append((event_id, stale or "retry_later"))
                continue
            if outcome == "unknown_payment":
                stale = self._finish_event(event_id, lease, "ignored", "unknown_payment")
            else:
                stale = self._finish_event(event_id, lease, "processed", None)
            results.append((event_id, stale or outcome))
        return results

    def _claim_event(self):
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE provider_events
                    SET status = 'processing',
                        attempts = attempts + 1,
                        lease_token = gen_random_uuid(),
                        lease_expires_at = now() + interval '1 second' * {int(self.event_lease_seconds)},
                        recheck_requested = FALSE
                    WHERE id = (
                        SELECT id FROM provider_events
                        WHERE provider = %s
                          AND ((status IN ('received', 'failed') AND next_attempt_at <= now())
                               OR (status = 'processing' AND lease_expires_at <= now()))
                        ORDER BY next_attempt_at, id
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id, event_type, object_id, attempts, lease_token::text
                    """,
                    (self.provider.name,),
                )
                return cursor.fetchone()

    def _finish_event(self, event_id, lease, status, note):
        """Завершить ТОЛЬКО свою аренду. None — записано; 'stale_lease' — аренда уже не наша.

        Если во время обработки пришла повторная доставка (recheck_requested), событие
        возвращается в очередь (received) и будет перепроверено у провайдера.
        """
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE provider_events
                    SET status = CASE WHEN recheck_requested THEN 'received' ELSE %s END,
                        last_error = CASE WHEN recheck_requested THEN NULL ELSE %s END,
                        processed_at = CASE WHEN recheck_requested THEN processed_at ELSE now() END,
                        next_attempt_at = now(),
                        lease_token = NULL, lease_expires_at = NULL, recheck_requested = FALSE
                    WHERE id = %s AND status = 'processing' AND lease_token = %s::uuid
                    RETURNING id
                    """,
                    (status, note, event_id, lease),
                )
                done = cursor.fetchone() is not None
        if not done:
            log.warning("уведомление %s: аренда истекла и перешла другому обработчику — результат не записан",
                        event_id)
            return "stale_lease"
        return None

    def _fail_event(self, event_id, lease, attempts, reason):
        delay = min(30 * 2 ** (max(attempts, 1) - 1), EVENT_MAX_BACKOFF_SECONDS)
        log.warning("уведомление %s: провайдер недоступен (%s), повтор через %s с", event_id, reason, delay)
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE provider_events
                    SET status = 'failed', last_error = %s,
                        next_attempt_at = now() + interval '1 second' * {int(delay)},
                        lease_token = NULL, lease_expires_at = NULL, recheck_requested = FALSE
                    WHERE id = %s AND status = 'processing' AND lease_token = %s::uuid
                    RETURNING id
                    """,
                    (reason[:200], event_id, lease),
                )
                done = cursor.fetchone() is not None
        return None if done else "stale_lease"

    # ---------- сверка ----------

    def reconcile(self, limit=50):
        """Сверка: недосозданные попытки, «потерянные» попытки, незавершённые платежи, очередь."""
        self._require_enabled()
        report = {"recreated": 0, "unresolved": 0, "discovered": 0, "checked": 0, "errors": 0, "events": 0}

        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT id, order_id, idempotence_key::text, amount_minor, currency, status,
                           now() - created_at > interval '23 hours'
                    FROM payments
                    WHERE provider = %s AND status IN ('creating', 'unknown')
                      AND updated_at <= now() - interval '1 second' * {int(self.stale_attempt_grace_seconds)}
                    ORDER BY id
                    LIMIT %s
                    """,
                    (self.provider.name, limit),
                )
                stale = cursor.fetchall()
        for row in stale:
            attempt = _Attempt(row[0], row[1], row[2], row[3], row[4], row[5], None, bool(row[6]))
            if attempt.expired:
                log.error("платёж %s: результат создания неизвестен дольше срока ключа — нужна проверка",
                          attempt.payment_id)
                self._mark(attempt.payment_id, "requires_action", "unresolved_attempt")
                report["unresolved"] += 1
                continue
            try:
                self._create_and_bind(attempt)
                report["recreated"] += 1
            except PaymentServiceError:
                report["errors"] += 1

        report["discovered"] = sum(self._search_unresolved(limit))

        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT provider_payment_id FROM payments
                    WHERE provider = %s AND provider_payment_id IS NOT NULL
                      AND (status IN ('pending', 'waiting_for_capture')
                           -- найденная «потерянная» попытка без пригодной ссылки: ждём отмены у провайдера
                           OR (status = 'requires_action' AND status_reason = 'unresolved_attempt'
                               AND resolved_at IS NULL))
                      AND (last_checked_at IS NULL
                           OR last_checked_at <= now() - interval '1 second' * {int(self.open_payment_recheck_seconds)})
                    ORDER BY id
                    LIMIT %s
                    """,
                    (self.provider.name, limit),
                )
                open_payments = [row[0] for row in cursor.fetchall()]
        for provider_payment_id in open_payments:
            try:
                self.sync_provider_payment(provider_payment_id)
                report["checked"] += 1
            except ProviderError as error:
                log.warning("сверка платежа: %s", type(error).__name__)
                report["errors"] += 1

        report["events"] = len(self.process_due_events(limit))
        return report

    def _search_unresolved(self, limit):
        """«Потерянные» попытки: ищем платёж по metadata в списке платежей провайдера.

        Нашёлся — дальше обычная обработка (привязка, сверка, учёт). Не нашёлся — попытка
        остаётся requires_action: отсутствие в списке не считается доказательством, решение
        о новой оплате принимает человек (admin_resolve_payment).
        """
        with self._connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT id, order_id,
                           to_char((created_at - interval '10 minutes') AT TIME ZONE 'UTC',
                                   'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"'),
                           to_char((created_at + interval '25 hours') AT TIME ZONE 'UTC',
                                   'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"')
                    FROM payments
                    WHERE provider = %s AND status = 'requires_action' AND resolved_at IS NULL
                      AND status_reason = 'unresolved_attempt' AND provider_payment_id IS NULL
                      AND created_at > now() - interval '{UNRESOLVED_SEARCH_DAYS} days'
                      AND (last_checked_at IS NULL
                           OR last_checked_at <= now() - interval '1 second' * {int(self.unresolved_recheck_seconds)})
                    ORDER BY id
                    LIMIT %s
                    """,
                    (self.provider.name, limit),
                )
                rows = cursor.fetchall()
        outcomes = []
        for payment_id, order_id, created_gte, created_lt in rows:
            try:
                payment = self.provider.find_payment_by_metadata(
                    payment_attempt=payment_id, order_id=order_id, created_gte=created_gte, created_lt=created_lt)
            except ProviderError as error:
                log.warning("поиск попытки %s у провайдера: %s", payment_id, type(error).__name__)
                outcomes.append(False)
                continue
            if payment is None:
                self._scalar("SELECT payment_attempt_touch(%s, NULL)::text", (payment_id,))
                log.warning("попытка %s: платёж у провайдера не найден — ждёт решения администратора", payment_id)
                outcomes.append(False)
                continue
            log.warning("попытка %s: платёж найден у провайдера (%s)", payment_id, payment.status)
            self._apply_provider_payment(payment)
            outcomes.append(True)
        return outcomes


def _fulfillment_summary(statuses):
    if not statuses:
        return "not_started"
    if any(s in ("requires_action", "failed") for s in statuses):
        return "requires_action"
    if all(s == "fulfilled" for s in statuses):
        return "fulfilled"
    if all(s == "cancelled" for s in statuses):
        return "cancelled"
    if any(s in ("fulfilled", "in_progress") for s in statuses):
        return "in_progress"
    return "pending"


def _refund_summary(statuses):
    if not statuses:
        return "none"
    if any(s in ("requested", "pending", "requires_action") for s in statuses):
        return "pending"
    if all(s == "canceled" for s in statuses):
        return "canceled"
    return "succeeded"
