"""Контракт исполнения заказа после подтверждённой оплаты.

Как это устроено:
* При подтверждённой оплате (payment_service.apply_verified_payment) для каждой
  позиции заказа в той же транзакции создаётся строка fulfillments со статусом
  "pending". Это и есть состояние «оплачено, но ещё не исполнено».
* Модуль исполнения (digital_keys, downloads, physical_shipping, manual) реализует
  FulfillmentHandler и регистрируется через register_handler().
* process_pending_fulfillments() берёт по одной задаче (FOR UPDATE SKIP LOCKED),
  вызывает модуль в той же транзакции и сохраняет результат.

Гарантии:
* "fulfilled" ставится ТОЛЬКО если модуль вернул FulfillmentResult.fulfilled(...),
  то есть сообщил о реально совершённом действии. Нет модуля — задача остаётся
  "pending" (в PR 2 модулей нет: исполнение — следующий этап).
* Одна задача на позицию (UNIQUE order_item_id) и блокировка строки: два воркера
  не выполнят одну задачу одновременно. Модуль обязан быть идемпотентным по task.id
  (повтор после сбоя не должен выдать второй ключ).
* Неудачная попытка откладывает следующую (next_attempt_at, экспоненциальная пауза:
  retry_delay_seconds, ×2, …, не больше часа). Ни этот, ни другой воркер не возьмёт
  задачу раньше — повтор не выполняется сразу и параллельно.
* Исключение модуля откатывает его изменения; в задаче сохраняется только имя
  класса исключения или публичное сообщение FulfillmentError — никаких секретов
  (ключей, ссылок, токенов) в базе ошибок и логах. После max_attempts — "failed".

Транзакционные границы одной попытки (одна транзакция на задачу):
    BEGIN
      SELECT ... FOR UPDATE SKIP LOCKED      -- захват задачи; блокировка до COMMIT
      SAVEPOINT fulfillment_attempt
        handler.fulfill(conn, task)          -- изменения модуля
      успех:  RELEASE SAVEPOINT; UPDATE статус
      ошибка: ROLLBACK TO SAVEPOINT          -- откат ТОЛЬКО изменений модуля;
              UPDATE attempts/last_error     -- блокировка задачи по-прежнему удерживается
    COMMIT                                   -- результат и снятие блокировки одновременно
Конкурирующий воркер не может взять задачу, пока результат попытки не записан.
Модулю запрещено вызывать conn.commit()/conn.rollback(): это разрушает границы.
Если модуль всё же это сделал, воркер не падает: он заново захватывает задачу
только если её никто не изменил (по числу попыток) и записывает попытку.
"""

from dataclasses import dataclass
from typing import Optional, Protocol

import psycopg

import database

FULFILLMENT_TYPES = ("manual", "digital_keys", "downloads", "physical_shipping")

_RESULT_STATUSES = ("fulfilled", "in_progress", "requires_action")


@dataclass(frozen=True)
class FulfillmentTask:
    """Задача исполнения одной позиции оплаченного заказа."""

    id: int
    order_id: int
    order_item_id: int
    fulfillment_type: str
    product_id: int
    quantity: int
    attempts: int


@dataclass(frozen=True)
class FulfillmentResult:
    status: str
    note: Optional[str] = None  # публичное пояснение, без секретов

    def __post_init__(self):
        if self.status not in _RESULT_STATUSES:
            raise ValueError(f"недопустимый результат исполнения: {self.status}")

    @classmethod
    def fulfilled(cls, note=None):
        """Действие реально выполнено (ключ выдан, файл открыт, задача создана и т. п.)."""
        return cls("fulfilled", note)

    @classmethod
    def in_progress(cls, note=None):
        """Действие начато во внешней системе (например, передано в доставку)."""
        return cls("in_progress", note)

    @classmethod
    def requires_action(cls, note=None):
        """Автоматически выполнить нельзя — нужен человек."""
        return cls("requires_action", note)


class FulfillmentError(Exception):
    """Ошибка модуля с публичным (безопасным для хранения) сообщением."""

    def __init__(self, public_message):
        super().__init__(public_message)
        self.public_message = public_message[:200]


class FulfillmentHandler(Protocol):
    fulfillment_type: str

    def fulfill(self, conn, task: FulfillmentTask) -> FulfillmentResult:
        """Выполнить задачу в транзакции conn. Обязан быть идемпотентным по task.id."""
        ...


_HANDLERS = {}


def register_handler(handler):
    if handler.fulfillment_type not in FULFILLMENT_TYPES:
        raise ValueError(f"неизвестный тип исполнения: {handler.fulfillment_type}")
    if handler.fulfillment_type in _HANDLERS:
        raise ValueError(f"модуль для {handler.fulfillment_type} уже зарегистрирован")
    _HANDLERS[handler.fulfillment_type] = handler


def unregister_handler(fulfillment_type):
    _HANDLERS.pop(fulfillment_type, None)


def registered_types():
    return sorted(_HANDLERS)


def _safe_error(exc):
    if isinstance(exc, FulfillmentError):
        return exc.public_message
    return f"{type(exc).__name__} (подробности — в журнале модуля)"


def _claim_next(cursor, types, skip_ids):
    cursor.execute(
        """
        SELECT f.id, i.order_id, f.order_item_id, f.fulfillment_type,
               i.product_id, i.quantity, f.attempts
        FROM fulfillments f
        JOIN order_items i ON i.id = f.order_item_id
        JOIN orders o ON o.id = i.order_id
        WHERE f.status = 'pending'
          AND o.status = 'paid'
          AND f.fulfillment_type = ANY(%s)
          AND (f.next_attempt_at IS NULL OR f.next_attempt_at <= now())
          AND NOT (f.id = ANY(%s))
        ORDER BY f.id
        LIMIT 1
        FOR UPDATE OF f SKIP LOCKED
        """,
        (types, skip_ids),
    )
    row = cursor.fetchone()
    return None if row is None else FulfillmentTask(*row)


MAX_RETRY_DELAY_SECONDS = 3600


def _retry_delay(attempts, base_seconds):
    return min(base_seconds * 2 ** (attempts - 1), MAX_RETRY_DELAY_SECONDS)


def _record_failure(conn, task, exc, max_attempts, retry_delay_seconds):
    """Записывает неудачную попытку. Обновляет задачу, только если её состояние не
    изменилось с момента захвата (защита от двойного учёта). Возвращает итоговый статус
    или None, если задачу уже обработал кто-то другой."""
    attempts = task.attempts + 1
    status = "failed" if attempts >= max_attempts else "pending"
    with conn.cursor() as cursor:
        cursor.execute(
            """
            UPDATE fulfillments
            SET status = %s,
                attempts = %s,
                last_error = %s,
                next_attempt_at = CASE WHEN %s = 'pending'
                                       THEN now() + make_interval(secs => %s)
                                       ELSE NULL END,
                updated_at = now()
            WHERE id = %s AND status = 'pending' AND attempts = %s
            RETURNING id
            """,
            (status, attempts, _safe_error(exc), status, _retry_delay(attempts, retry_delay_seconds),
             task.id, task.attempts),
        )
        updated = cursor.fetchone() is not None
    return status if updated else None


def _relock_unchanged(conn, task):
    """Повторный захват задачи после нарушения контракта модулем (он завершил транзакцию)."""
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT id FROM fulfillments
            WHERE id = %s AND status = 'pending' AND attempts = %s
            FOR UPDATE SKIP LOCKED
            """,
            (task.id, task.attempts),
        )
        return cursor.fetchone() is not None


def process_pending_fulfillments(limit=20, max_attempts=5, retry_delay_seconds=30):
    """Выполняет до limit ожидающих задач. Возвращает счётчики по итоговым статусам.

    За один запуск каждая задача обрабатывается не больше одного раза. Неудачная попытка
    откладывает задачу на retry_delay_seconds × 2^(попытка-1) (не больше часа); до этого
    времени задачу не возьмёт ни этот, ни другой воркер.
    """
    summary = {"fulfilled": 0, "in_progress": 0, "requires_action": 0, "retry": 0, "failed": 0}
    types = registered_types()
    if not types:
        return summary
    attempted = [0]  # id задач, уже обработанных в этом запуске (0 — заглушка, id начинаются с 1)

    for _ in range(limit):
        conn = database.get_connection()
        try:
            with conn.cursor() as cursor:
                task = _claim_next(cursor, types, attempted)
            if task is None:
                conn.rollback()
                break
            attempted.append(task.id)

            handler = _HANDLERS[task.fulfillment_type]
            with conn.cursor() as cursor:
                cursor.execute("SAVEPOINT fulfillment_attempt")
            try:
                result = handler.fulfill(conn, task)
                if not isinstance(result, FulfillmentResult):
                    raise TypeError("модуль исполнения должен вернуть FulfillmentResult")
                with conn.cursor() as cursor:
                    cursor.execute("RELEASE SAVEPOINT fulfillment_attempt")
            except Exception as exc:  # noqa: BLE001 — любой сбой модуля не должен ронять воркер
                try:
                    # Откат только изменений модуля; блокировка задачи остаётся (взята до SAVEPOINT).
                    with conn.cursor() as cursor:
                        cursor.execute("ROLLBACK TO SAVEPOINT fulfillment_attempt")
                except psycopg.Error:
                    # Модуль сам завершил транзакцию (нарушение контракта): точки сохранения нет.
                    conn.rollback()
                    if not _relock_unchanged(conn, task):
                        conn.rollback()
                        continue
                status = _record_failure(conn, task, exc, max_attempts, retry_delay_seconds)
                conn.commit()
                if status is not None:
                    summary["failed" if status == "failed" else "retry"] += 1
                continue

            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE fulfillments
                    SET status = %s,
                        attempts = attempts + 1,
                        last_error = NULL,
                        next_attempt_at = NULL,
                        updated_at = now(),
                        fulfilled_at = CASE WHEN %s = 'fulfilled' THEN now() ELSE NULL END
                    WHERE id = %s
                    """,
                    (result.status, result.status, task.id),
                )
            conn.commit()
            summary[result.status] += 1
        finally:
            conn.close()

    return summary
