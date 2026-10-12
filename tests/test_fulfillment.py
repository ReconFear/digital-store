"""Контракт исполнения заказа (commerce/fulfillment.py).

Настоящих модулей исполнения в PR 2 нет; здесь — поддельные модули, только для проверки
гарантий: нет модуля — нет «выполнено», ошибки без секретов, повторы, один исполнитель на задачу.
"""

import threading

import pytest

import payment_service
from commerce import fulfillment
from commerce.fulfillment import FulfillmentError, FulfillmentResult
from commerce.orders import create_order

SECRET = "KEY-AAAA-BBBB-CCCC"


class FakeHandler:
    def __init__(self, fulfillment_type, behaviour):
        self.fulfillment_type = fulfillment_type
        self.behaviour = behaviour
        self.calls = []
        self.lock = threading.Lock()

    def fulfill(self, conn, task):
        with self.lock:
            self.calls.append(task.id)
        return self.behaviour(task)


@pytest.fixture
def handler():
    registered = []

    def install(fulfillment_type, behaviour):
        h = FakeHandler(fulfillment_type, behaviour)
        fulfillment.register_handler(h)
        registered.append(fulfillment_type)
        return h

    yield install
    for fulfillment_type in registered:
        fulfillment.unregister_handler(fulfillment_type)


@pytest.fixture
def paid_order(make_product):
    def factory(fulfillment_type="manual", product_type="other", count=1):
        items = []
        for _ in range(count):
            pid = make_product(quantity=5, product_type=product_type, fulfillment_type=fulfillment_type)
            items.append((pid, 1))
        order = create_order(items)
        assert payment_service.apply_verified_payment(order["id"])["status"] == "paid"
        return order["id"]

    return factory


def _tasks(db_value, order_id):
    """Состояния задач заказа: ["status:attempts:last_error", ...] или None, если задач нет."""
    joined = db_value(
        """
        SELECT string_agg(f.status || ':' || f.attempts || ':' || coalesce(f.last_error, ''), '|' ORDER BY f.id)
        FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id WHERE i.order_id = %s
        """,
        (order_id,),
    )
    return None if joined is None else joined.split("|")


def test_without_modules_nothing_is_marked_fulfilled(paid_order, db_value):
    order_id = paid_order()

    assert fulfillment.registered_types() == []
    assert fulfillment.process_pending_fulfillments()["fulfilled"] == 0
    assert _tasks(db_value, order_id) == ["pending:0:"]


def test_fulfilled_only_when_module_reports_real_action(paid_order, handler, db_value):
    order_id = paid_order(fulfillment_type="digital_keys", product_type="digital_key")
    h = handler("digital_keys", lambda task: FulfillmentResult.fulfilled("ключ выдан"))

    summary = fulfillment.process_pending_fulfillments()

    assert summary["fulfilled"] == 1 and len(h.calls) == 1
    assert _tasks(db_value, order_id) == ["fulfilled:1:"]
    assert db_value("""SELECT count(*) FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id
                       WHERE i.order_id = %s AND f.fulfilled_at IS NOT NULL""", (order_id,)) == 1
    # Повторный запуск ничего не делает: задача уже выполнена.
    fulfillment.process_pending_fulfillments()
    assert len(h.calls) == 1


@pytest.mark.parametrize("result, status", [
    (FulfillmentResult.in_progress(), "in_progress"),
    (FulfillmentResult.requires_action("нужен оператор"), "requires_action"),
])
def test_non_final_results(paid_order, handler, db_value, result, status):
    order_id = paid_order()
    handler("manual", lambda task: result)

    fulfillment.process_pending_fulfillments()

    assert _tasks(db_value, order_id) == [f"{status}:1:"]


def test_errors_are_retried_and_never_store_secrets(paid_order, handler, db_value):
    order_id = paid_order(fulfillment_type="digital_keys", product_type="digital_key")

    def leak(task):
        raise RuntimeError(f"не удалось выдать {SECRET}")

    handler("digital_keys", leak)

    summary = fulfillment.process_pending_fulfillments(max_attempts=3, retry_delay_seconds=0)
    (state,) = _tasks(db_value, order_id)
    assert summary["retry"] == 1  # одна попытка за запуск, без немедленного повтора
    assert state.startswith("pending:1:RuntimeError")
    assert SECRET not in state

    fulfillment.process_pending_fulfillments(max_attempts=3, retry_delay_seconds=0)
    fulfillment.process_pending_fulfillments(max_attempts=3, retry_delay_seconds=0)
    (state,) = _tasks(db_value, order_id)
    assert state.startswith("failed:3:")
    assert SECRET not in state


def test_public_error_message_is_kept(paid_order, handler, db_value):
    order_id = paid_order()

    def fail(task):
        raise FulfillmentError("склад временно недоступен")

    handler("manual", fail)
    fulfillment.process_pending_fulfillments()

    assert _tasks(db_value, order_id) == ["pending:1:склад временно недоступен"]


def test_invalid_module_result_is_treated_as_error(paid_order, handler, db_value):
    order_id = paid_order()
    handler("manual", lambda task: "fulfilled")  # строка вместо FulfillmentResult

    fulfillment.process_pending_fulfillments()

    assert _tasks(db_value, order_id)[0].startswith("pending:1:TypeError")


def test_unpaid_orders_are_not_fulfilled(make_product, handler, db_value):
    pid = make_product(quantity=5)
    order = create_order([(pid, 1)])  # не оплачен
    h = handler("manual", lambda task: FulfillmentResult.fulfilled())

    fulfillment.process_pending_fulfillments()

    assert h.calls == []
    assert _tasks(db_value, order["id"]) is None


def test_parallel_workers_execute_each_task_once(paid_order, handler):
    order_ids = [paid_order(count=2) for _ in range(3)]
    h = handler("manual", lambda task: FulfillmentResult.fulfilled())

    barrier = threading.Barrier(4)

    def worker():
        barrier.wait(timeout=10)
        fulfillment.process_pending_fulfillments(limit=10)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(order_ids) * 2 == len(h.calls) == len(set(h.calls))


def test_registry_rejects_unknown_and_duplicate_types(handler):
    handler("manual", lambda task: FulfillmentResult.fulfilled())

    with pytest.raises(ValueError):
        fulfillment.register_handler(FakeHandler("teleport", None))
    with pytest.raises(ValueError):
        fulfillment.register_handler(FakeHandler("manual", None))
    with pytest.raises(ValueError):
        FulfillmentResult("done")


# ---------- гонка при ошибке модуля (ревью PR 2) ----------

def test_competing_worker_cannot_take_task_while_failure_is_recorded(paid_order, handler, db_value, monkeypatch):
    """Второй воркер запускается ровно в промежутке между откатом изменений модуля и записью
    неудачной попытки. Задача в этот момент обязана оставаться заблокированной."""
    order_id = paid_order()

    def boom(task):
        raise RuntimeError("boom")

    h = handler("manual", boom)
    original_safe_error = fulfillment._safe_error
    competitor = {}

    def safe_error_with_competitor(exc):
        if not competitor:
            competitor["started"] = True
            competitor["summary"] = fulfillment.process_pending_fulfillments()
        return original_safe_error(exc)

    monkeypatch.setattr(fulfillment, "_safe_error", safe_error_with_competitor)

    first = fulfillment.process_pending_fulfillments()

    assert competitor["summary"]["retry"] == 0      # конкурент задачу не получил
    assert first["retry"] == 1
    assert len(h.calls) == 1                         # модуль вызван один раз
    (state,) = _tasks(db_value, order_id)
    assert state.startswith("pending:1:RuntimeError")


def test_parallel_failing_workers_count_each_attempt_once(paid_order, handler, db_value):
    import time

    order_ids = [paid_order(count=2) for _ in range(3)]

    def slow_failure(task):
        time.sleep(0.05)
        raise RuntimeError("boom")

    h = handler("manual", slow_failure)
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait(timeout=10)
        fulfillment.process_pending_fulfillments(limit=10)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(h.calls) == len(set(h.calls)) == 6    # каждая задача — ровно одна попытка
    for order_id in order_ids:
        assert [s.split(":")[:2] for s in _tasks(db_value, order_id)] == [["pending", "1"], ["pending", "1"]]


def test_database_error_inside_module_is_rolled_back_to_savepoint(paid_order, make_product, handler, db_value):
    """Модуль успел изменить данные, затем получил настоящую ошибку PostgreSQL
    (транзакция в состоянии aborted). Его изменения откатываются, попытка записывается."""
    order_id = paid_order()
    witness = make_product(quantity=7)

    def partial_then_sql_error(conn, task):
        with conn.cursor() as cursor:
            cursor.execute("UPDATE products SET quantity = 0 WHERE id = %s", (witness,))
            cursor.execute("SELECT 1 / 0")
        return FulfillmentResult.fulfilled()

    h = handler("manual", None)
    h.fulfill = partial_then_sql_error

    summary = fulfillment.process_pending_fulfillments()

    assert summary["retry"] == 1
    assert db_value("SELECT quantity FROM products WHERE id = %s", (witness,)) == 7
    (state,) = _tasks(db_value, order_id)
    assert state.startswith("pending:1:")


def test_module_that_breaks_transaction_contract_does_not_break_worker(paid_order, handler, db_value):
    """Модуль не имеет права делать COMMIT/ROLLBACK. Если сделал — воркер не падает,
    попытка всё равно учитывается ровно один раз."""
    order_id = paid_order()

    def commit_then_fail(conn, task):
        conn.commit()
        raise RuntimeError("boom")

    h = handler("manual", None)
    h.fulfill = commit_then_fail

    summary = fulfillment.process_pending_fulfillments()

    assert summary["retry"] == 1
    (state,) = _tasks(db_value, order_id)
    assert state.startswith("pending:1:RuntimeError")


def test_failed_attempt_is_delayed_for_every_worker(paid_order, handler, db_value):
    order_id = paid_order()

    def boom(task):
        raise RuntimeError("boom")

    h = handler("manual", boom)

    fulfillment.process_pending_fulfillments(retry_delay_seconds=60)
    again = fulfillment.process_pending_fulfillments(retry_delay_seconds=60)  # «другой воркер» сразу после

    assert again["retry"] == 0 and len(h.calls) == 1
    delay = db_value(
        """SELECT extract(epoch FROM f.next_attempt_at - f.updated_at)::int
           FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id WHERE i.order_id = %s""",
        (order_id,),
    )
    assert delay == 60


def test_retry_delay_grows_and_is_capped():
    assert [fulfillment._retry_delay(n, 30) for n in (1, 2, 3, 4)] == [30, 60, 120, 240]
    assert fulfillment._retry_delay(20, 30) == fulfillment.MAX_RETRY_DELAY_SECONDS


def test_parallel_workers_with_database_errors_count_each_attempt_once(paid_order, handler, db_value):
    """Два воркера параллельно; модуль получает НАСТОЯЩУЮ ошибку PostgreSQL (транзакция aborted).
    Каждая задача — ровно одна попытка, счётчик = 1, изменения модуля откатаны."""
    order_ids = [paid_order(count=2) for _ in range(2)]
    calls = []
    lock = threading.Lock()

    def sql_error(conn, task):
        with lock:
            calls.append(task.id)
        with conn.cursor() as cursor:
            cursor.execute("UPDATE fulfillments SET last_error = 'должно откатиться' WHERE id = %s", (task.id,))
            cursor.execute("SELECT pg_sleep(0.05)")
            cursor.execute("SELECT 1 / 0")
        return FulfillmentResult.fulfilled()

    h = handler("manual", None)
    h.fulfill = sql_error
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait(timeout=10)
        fulfillment.process_pending_fulfillments(limit=10)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert len(calls) == len(set(calls)) == 4
    for order_id in order_ids:
        for state in _tasks(db_value, order_id):
            status, attempts, error = state.split(":", 2)
            assert (status, attempts) == ("pending", "1")
            assert "должно откатиться" not in error
