"""PR5: конкурентность на настоящем PostgreSQL — независимые соединения и управляемое чередование.

Схема каждого теста: соединение A выполняет шаг и держит транзакцию открытой; соединение B в
отдельном потоке выполняет конфликтующий шаг; ожидание B на блокировке подтверждается по
pg_stat_activity (wait_event_type = 'Lock' для pid соединения B); затем A фиксирует или
откатывает, и проверяется результат B. lock_timeout = 5 с: зависание — падение теста, а не вечное
ожидание. Взаимоблокировка — SQLSTATE 40P01.

Порядок блокировок, который здесь проверяется (DIGITAL_KEYS.md): задача -> платёж -> заказ ->
товар -> advisory(товар) -> ключи.
"""

import random
import re
import threading
import time

import pytest

import database
from digital_keys_testkit import shop_admin, shop_admin_factory  # noqa: F401  (фикстуры)
from digital_keys_testkit import (
    invariant_holds, make_digital, new_order, one, pay, q, record_success, run_worker, start_payment, stock,
    stock_keys, tasks, unique_codes,
)

LOCK_TIMEOUT = "5s"


def _connect():
    conn = database.get_connection()
    with conn.cursor() as cur:
        cur.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
        cur.execute("SELECT pg_backend_pid()")
        pid = cur.fetchone()[0]
    conn.commit()
    return conn, pid


def _exec(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0] if cur.description else None


def _jsonb(value):
    import json
    return json.loads(value) if isinstance(value, str) else value


class Background:
    """Шаг соединения B в отдельном потоке."""

    def __init__(self, fn):
        self.result = self.error = None
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def _run(self, fn):
        try:
            self.result = fn()
        except BaseException as exc:  # noqa: BLE001
            self.error = exc

    def join(self, timeout=15):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "шаг B не завершился"
        return self


def wait_blocked(pid, timeout=5.0):
    """Ждёт, пока соединение pid встанет на ожидание блокировки. -> wait_event."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # wait_event_type чужой роли виден только суперпользователю и pg_read_all_stats; ожидание
        # соединения временного администратора подтверждается через pg_blocking_pids().
        row = q("SELECT wait_event_type, wait_event, cardinality(pg_blocking_pids(%s)) FROM pg_stat_activity "
                "WHERE pid = %s", (pid, pid))
        if row and row[0][0] == "Lock":
            return row[0][1]
        if row and row[0][0] is None and row[0][2]:
            return "lock (по pg_blocking_pids)"
        time.sleep(0.01)
    raise AssertionError(f"соединение {pid} не встало на ожидание блокировки")


@pytest.fixture
def keys_module():
    """Модуль digital_keys зарегистрирован на весь тест (реестр модулей — общий для потоков)."""
    from commerce import fulfillment
    from commerce.digital_keys.handler import DigitalKeysHandler
    fulfillment.register_handler(DigitalKeysHandler())
    yield lambda **kw: fulfillment.process_pending_fulfillments(retry_delay_seconds=0, **kw)
    fulfillment.unregister_handler("digital_keys")


@pytest.fixture
def admin_conns(shop_admin):
    """Соединения персонального администратора (временная роль, admin_*-функции)."""
    opened = []

    def factory():
        conn = shop_admin.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            pid = cur.fetchone()[0]
        conn.commit()
        opened.append((conn, pid))
        return conn, pid

    yield factory
    pids = [pid for _, pid in opened]
    if pids:
        try:   # сеансы другой роли: завершить может не каждая тестовая роль — тогда просто закрываем
            q("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid = ANY(%s) AND state <> 'idle'",
              (pids,))
        except Exception:  # noqa: BLE001
            pass
    for conn, _ in opened:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def conns():
    opened = []

    def factory():
        conn, pid = _connect()
        opened.append((conn, pid))
        return conn, pid

    yield factory
    # Если тест упал, пока поток B ждал блокировку, его соединение занято: завершаем сеансы
    # на стороне сервера (свои сеансы той же роли), затем закрываем сокеты.
    pids = [pid for _, pid in opened]
    if pids:
        q("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE pid = ANY(%s) AND state <> 'idle'",
          (pids,))
    for conn, _ in opened:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _fid(order_id):
    return tasks(order_id)[0][0]


def _withdraw_sql(public_id):
    return ("SELECT digital_keys_withdraw_core(%s, 'pytest', 'pytest')", (public_id,))


def _free_key(pid, last=False):
    return one(f"SELECT public_id::text FROM digital_keys WHERE product_id = %s AND status = 'available' "
               f"ORDER BY id {'DESC' if last else ''} LIMIT 1", (pid,))


# ---------- оплата (PR3, apply_paid_order) ----------

def test_payment_waits_for_import_and_sees_new_keys(make_product, conns):
    pid, _ = make_digital(make_product, keys=1)
    first, _ = new_order([(pid, 1)])
    second, _ = new_order([(pid, 1)])           # создан при S = 1
    pay(first)                                  # теперь A − W = 0
    payment = start_payment(second)

    a, _ = conns()
    b, b_pid = conns()
    stock_keys(pid, 1, conn=a)                  # импорт держит строку товара
    job = Background(lambda: record_success(*payment, conn=b))
    assert wait_blocked(b_pid) in ("tuple", "transactionid")
    a.commit()
    job.join()
    assert job.error is None
    b.commit()
    assert job.result["order"]["status"] == "paid"
    assert invariant_holds(pid)


def test_two_payments_for_the_last_key(make_product, conns):
    pid, _ = make_digital(make_product, keys=1)
    first, _ = new_order([(pid, 1)])
    second, _ = new_order([(pid, 1)])
    p1, p2 = start_payment(first), start_payment(second)

    a, _ = conns()
    b, b_pid = conns()
    assert record_success(*p1, conn=a)["order"]["status"] == "paid"
    job = Background(lambda: record_success(*p2, conn=b))
    wait_blocked(b_pid)
    a.commit()
    job.join()
    b.commit()
    assert job.error is None and job.result["order"]["error"] == "out_of_stock"
    assert invariant_holds(pid)


@pytest.mark.parametrize("first", ["withdraw", "payment"])
def test_payment_and_withdraw_in_both_orders(make_product, conns, first):
    pid, _ = make_digital(make_product, keys=2)
    q("UPDATE products SET quantity = 1 WHERE id = %s", (pid,))
    order_id, _ = new_order([(pid, 1)])
    payment = start_payment(order_id)
    key = _free_key(pid, last=True)

    a, _ = conns()
    b, b_pid = conns()
    if first == "withdraw":
        _exec(a, *_withdraw_sql(key))
        job = Background(lambda: record_success(*payment, conn=b))
    else:
        record_success(*payment, conn=a)
        job = Background(lambda: _exec(b, *_withdraw_sql(key)))
    wait_blocked(b_pid)
    a.commit()
    job.join()
    assert job.error is None, job.error
    b.commit()
    assert one("SELECT status FROM orders WHERE id = %s", (order_id,)) == "paid"
    assert stock(pid)[3] == 1 and invariant_holds(pid)


def test_raise_quantity_waits_for_payment(make_product, conns):
    """Ручное повышение Q во время оплаты: проверка триггера видит итог оплаты, а не старый W."""
    pid, _ = make_digital(make_product, keys=2)
    q("UPDATE products SET quantity = 1 WHERE id = %s", (pid,))
    order_id, _ = new_order([(pid, 1)])
    payment = start_payment(order_id)

    a, _ = conns()
    b, b_pid = conns()
    record_success(*payment, conn=a)            # Q 1 -> 0, W 0 -> 1 (не зафиксировано)
    job = Background(lambda: _exec(b, "UPDATE products SET quantity = 2 WHERE id = %s", (pid,)))
    wait_blocked(b_pid)
    a.commit()
    job.join()
    b.rollback()
    assert job.error is not None and "больше обеспеченного" in str(job.error)   # 2 > A − W = 1
    assert invariant_holds(pid)


# ---------- выдача ----------

@pytest.mark.parametrize("first", ["assign", "withdraw"])
def test_assign_and_withdraw_no_false_shortage_no_double_use(make_product, conns, first):
    pid, _ = make_digital(make_product, keys=3)
    order_id, _ = new_order([(pid, 1)])
    pay(order_id)
    q("UPDATE products SET quantity = 0 WHERE id = %s", (pid,))   # A=3, W=1, Q=0: изъять можно
    fid = _fid(order_id)
    lowest = one("SELECT public_id::text FROM digital_keys WHERE product_id = %s ORDER BY id LIMIT 1", (pid,))

    a, _ = conns()
    b, b_pid = conns()
    if first == "assign":
        assert _jsonb(_exec(a, "SELECT digital_keys_assign(%s)", (fid,)))["result"] == "assigned"
        job = Background(lambda: _exec(b, *_withdraw_sql(lowest)))
        assert wait_blocked(b_pid) == "advisory"
        a.commit()
        job.join()
        b.rollback()
        # Самый «нижний» ключ ушёл покупателю: изъять его уже нельзя.
        assert job.error is not None and "только свободный" in str(job.error)
    else:
        # Изъятие держит advisory и строку самого «нижнего» ключа — того, что выдача взяла бы первым.
        _exec(a, *_withdraw_sql(lowest))
        job = Background(lambda: _jsonb(_exec(b, "SELECT digital_keys_assign(%s)", (fid,))))
        assert wait_blocked(b_pid) == "advisory"
        a.commit()
        job.join()
        assert job.error is None and job.result["result"] == "assigned"   # нет ложной нехватки
        b.commit()
    assert one("SELECT count(*) FROM digital_keys WHERE fulfillment_id = %s", (fid,)) == 1
    assert one("SELECT count(*) FROM digital_keys WHERE public_id = %s AND status = 'assigned'", (lowest,)) == \
        (1 if first == "assign" else 0)
    assert invariant_holds(pid)


def test_two_workers_on_the_last_key(make_product, conns):
    pid, _ = make_digital(make_product, keys=2)
    o1, _ = new_order([(pid, 1)])
    o2, _ = new_order([(pid, 1)])
    pay(o1)
    pay(o2)
    # После оплаты один ключ пропал (например, изъят в обход учёта): ключей меньше, чем ждут.
    q("UPDATE digital_keys SET status = 'withdrawn', withdrawn_at = now(), withdraw_reason = 'pytest' "
      "WHERE id = (SELECT max(id) FROM digital_keys WHERE product_id = %s)", (pid,))

    a, _ = conns()
    b, b_pid = conns()
    assert _jsonb(_exec(a, "SELECT digital_keys_assign(%s)", (_fid(o1),)))["result"] == "assigned"
    job = Background(lambda: _jsonb(_exec(b, "SELECT digital_keys_assign(%s)", (_fid(o2),))))
    assert wait_blocked(b_pid) == "advisory"
    a.commit()
    job.join()
    b.commit()
    assert job.result == {"result": "insufficient", "need": 1, "available": 0}
    assert one("SELECT count(*) FROM digital_keys WHERE fulfillment_id = %s", (_fid(o2),)) == 0


def test_parallel_real_workers_never_double_assign(make_product, keys_module):
    pid, codes = make_digital(make_product, keys=12)
    orders = [new_order([(pid, 1 + i % 3)])[0] for i in range(6)]      # 1+2+3+1+2+3 = 12
    for order_id in orders:
        pay(order_id)
    jobs = [Background(lambda: keys_module(limit=10)) for _ in range(4)]
    for job in jobs:
        job.join(60)
        assert job.error is None, job.error
    keys_module()
    assert all(tasks(o)[0][1] == "fulfilled" for o in orders)
    assert one("SELECT count(DISTINCT id) FROM digital_keys WHERE product_id = %s AND status = 'assigned'",
               (pid,)) == 12
    assert q("SELECT f.id FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id "
             "WHERE i.product_id = %s AND (SELECT count(*) FROM digital_keys k WHERE k.fulfillment_id = f.id) "
             "<> i.quantity", (pid,)) == []


def test_crash_before_commit_returns_keys(make_product):
    pid, _ = make_digital(make_product, keys=2)
    order_id, _ = new_order([(pid, 2)])
    pay(order_id)
    conn, _ = _connect()
    assert _jsonb(_exec(conn, "SELECT digital_keys_assign(%s)", (_fid(order_id),)))["result"] == "assigned"
    conn.close()                                # соединение оборвано до COMMIT
    assert stock(pid)[1:3] == (2, 0)
    run_worker()
    assert tasks(order_id)[0][1] == "fulfilled"


@pytest.mark.parametrize("order", ["refund_first", "assign_first"])
def test_assign_and_refund_request(make_product, conns, order):
    pid, _ = make_digital(make_product, keys=1)
    order_id, _ = new_order([(pid, 1)])
    payment_id, _ = pay(order_id)
    fid = _fid(order_id)

    a, _ = conns()
    b, b_pid = conns()
    if order == "refund_first":
        _exec(a, "SELECT request_refund(%s, 100, 'pytest')", (payment_id,))
        job = Background(lambda: _jsonb(_exec(b, "SELECT digital_keys_assign(%s)", (fid,))))
    else:
        assert _jsonb(_exec(a, "SELECT digital_keys_assign(%s)", (fid,)))["result"] == "assigned"
        job = Background(lambda: _exec(b, "SELECT request_refund(%s, 100, 'pytest')", (payment_id,)))
    wait_blocked(b_pid)
    a.commit()
    job.join()
    assert job.error is None, job.error
    b.commit()
    if order == "refund_first":
        assert job.result == {"result": "refund_hold"}
        assert stock(pid)[2] == 0
    else:
        assert stock(pid)[2] == 1 and one("SELECT count(*) FROM refunds WHERE payment_id = %s", (payment_id,)) == 1


@pytest.mark.parametrize("order", ["assign_first", "cancel_first"])
def test_assign_and_cancel(make_product, conns, admin_conns, order):
    pid, _ = make_digital(make_product, keys=1)
    order_id, _ = new_order([(pid, 1)])
    pay(order_id)
    fid = _fid(order_id)
    cancel = ("SELECT admin_cancel_fulfillment(%s, 'pytest')", (fid,))   # под персональным администратором

    if order == "assign_first":
        a, _ = conns()
        b, b_pid = admin_conns()
        _exec(a, "SELECT digital_keys_assign(%s)", (fid,))
        _exec(a, "UPDATE fulfillments SET status = 'fulfilled', fulfilled_at = now() WHERE id = %s", (fid,))
        job = Background(lambda: _exec(b, *cancel))
    else:
        a, _ = admin_conns()
        b, b_pid = conns()
        _exec(a, *cancel)
        job = Background(lambda: _jsonb(_exec(b, "SELECT digital_keys_assign(%s)", (fid,))))
    wait_blocked(b_pid)
    a.commit()
    job.join()
    b.rollback() if job.error else b.commit()
    if order == "assign_first":
        assert job.error is not None and "выданное не отзывается" in str(job.error)
        assert tasks(order_id)[0][1] == "fulfilled"
    else:
        assert job.error is None and job.result["result"] == "not_pending"
        assert tasks(order_id)[0][1] == "cancelled" and stock(pid)[2] == 0


def test_concurrent_resumes_only_one_wins(make_product, admin_conns):
    pid, _ = make_digital(make_product, keys=1)
    order_id, _ = new_order([(pid, 1)])
    pay(order_id)
    fid = _fid(order_id)
    q("UPDATE fulfillments SET status = 'requires_action' WHERE id = %s", (fid,))   # вход в requires_action разрешён
    resume = ("SELECT admin_resume_fulfillment(%s, 'pytest', FALSE)", (fid,))

    a, _ = admin_conns()
    b, b_pid = admin_conns()
    _exec(a, *resume)
    job = Background(lambda: _exec(b, *resume))
    wait_blocked(b_pid)
    a.commit()
    job.join()
    b.rollback()
    assert job.error is not None and "requires_action или failed" in str(job.error)


def test_same_code_imported_into_two_products_concurrently(make_product, conns):
    """Импорты выстроены глобальной блокировкой импорта: второй ждёт первый, видит его ключ и
    получает обычный отчёт о дубликате (а не ошибку уникальности и не взаимоблокировку)."""
    p1, _ = make_digital(make_product)
    p2, _ = make_digital(make_product)
    code = unique_codes(1)

    a, _ = conns()
    b, b_pid = conns()
    stock_keys(p1, codes=code, conn=a)
    job = Background(lambda: stock_keys(p2, codes=code, conn=b))
    assert wait_blocked(b_pid) == "advisory"
    a.commit()
    job.join()
    assert job.error is None, job.error
    b.commit()
    assert job.result[1]["status"] == "rejected" and job.result[1]["duplicates_existing"] == [1]
    assert stock(p1)[1] == 1 and stock(p2)[1] == 0


def test_concurrent_imports_under_two_fingerprint_versions(make_product, conns):
    """Одновременный импорт под двумя версиями секрета отпечатков: второй ждёт первый и отклоняется
    (иначе на изначально пустом складе могли бы появиться две версии)."""
    import base64, hashlib
    from commerce.digital_keys import importer
    from commerce.digital_keys.crypto import Keyring
    from digital_keys_testkit import TEST_ENV, register_test_keys
    p1, _ = make_digital(make_product)
    p2, _ = make_digital(make_product)
    other = Keyring.from_env(dict(TEST_ENV, DIGITAL_KEYS_FINGERPRINT_KEY="tf9:" + base64.b64encode(
        hashlib.sha256(b"pytest-only/fp-tf9").digest()).decode()), need_active=True, need_fingerprint=True)

    a, _ = conns()
    b, b_pid = conns()
    with b.cursor() as cur:
        value = other.check_values()["fingerprint:tf9"]
        cur.execute("SELECT encryption_key_register_core('fingerprint', 'tf9', %s, 'pytest')", (value,))
        register_test_keys(cur)
    b.commit()
    stock_keys(p1, codes=unique_codes(1), conn=a)

    def import_other():
        batch = importer.prepare_batch(other, p2, [(1, unique_codes(1)[0])])
        with b.cursor() as cur:
            return importer.submit(cur, batch, "pytest", core_actor="pytest")

    job = Background(import_other)
    assert wait_blocked(b_pid) == "advisory"
    a.commit()
    job.join()
    b.rollback()
    assert job.error is not None and "версии tf1" in str(job.error)
    assert stock(p2)[1] == 0


# ---------- стресс: случайное чередование всех операций ----------

def test_stress_random_interleaving_has_no_deadlocks_and_keeps_invariants(make_product, keys_module, request):
    # Административные операции — только через admin_*-функции под временной персональной ролью.
    # Без DS_TEST_CREATE_ROLES=1 они исключаются из смеси (это видно в выводе stats["admin_ops"]).
    try:
        admin = request.getfixturevalue("shop_admin")
    except pytest.skip.Exception:
        admin = None
    rng = random.Random(int(__import__("os").getenv("DS_STRESS_SEED", "20261013")))
    products = [make_digital(make_product, keys=6)[0] for _ in range(3)]
    stats = {"ok": 0, "business_error": 0, "deadlock": 0, "lock_timeout": 0, "other": [],
             "admin_ops": admin is not None, "ok_by_op": {}}
    lock = threading.Lock()
    paid_orders = []

    def op_order_and_pay(r):
        pid = r.choice(products)
        try:
            order_id, _ = new_order([(pid, r.randint(1, 2))])
        except Exception:  # нет продаваемого остатка — нормальный отказ
            return "business"
        payment_id, _ = pay(order_id)
        with lock:
            paid_orders.append((order_id, payment_id))

    def op_worker(r):
        keys_module(limit=3)

    def op_import(r):
        stock_keys(r.choice(products), r.randint(1, 3))

    def op_withdraw(r):
        pid = r.choice(products)
        key = _free_key(pid, last=r.random() < 0.5)
        if key is None:
            return "business"
        q("UPDATE products SET quantity = GREATEST(quantity - 1, 0) WHERE id = %s", (pid,))
        admin.call("SELECT admin_withdraw_digital_key(%s, 'stress')", (key,))

    def op_refund(r):
        with lock:
            if not paid_orders:
                return "business"
            order_id, payment_id = r.choice(paid_orders)
        one("SELECT request_refund(%s, 1, 'stress')", (payment_id,))

    def op_order_pay_refund(r):
        # Возврат запрошен сразу после оплаты — выдача уйдёт в requires_action (refund_hold),
        # чтобы возобновление и отмена в смеси работали с настоящими задачами.
        pid = r.choice(products)
        try:
            order_id, _ = new_order([(pid, 1)])
        except Exception:
            return "business"
        payment_id, _ = pay(order_id)
        one("SELECT request_refund(%s, 1, 'stress')", (payment_id,))

    def op_cancel(r):
        fid = one("SELECT f.id FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id "
                  "WHERE i.product_id = ANY(%s) AND f.status IN ('pending', 'requires_action') "
                  "ORDER BY random() LIMIT 1", (products,))
        if fid is None:
            return "business"
        admin.call("SELECT admin_cancel_fulfillment(%s, 'stress')", (fid,))

    def op_resume(r):
        fid = one("SELECT f.id FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id "
                  "WHERE i.product_id = ANY(%s) AND f.status = 'requires_action' ORDER BY random() LIMIT 1",
                  (products,))
        if fid is None:
            return "business"
        admin.call("SELECT admin_resume_fulfillment(%s, 'stress', %s)", (fid, r.random() < 0.5))

    def op_quantity(r):
        pid = r.choice(products)
        q("UPDATE products SET quantity = GREATEST(quantity + %s, 0) WHERE id = %s", (r.choice([-1, 1, 2]), pid))

    def op_reconcile(r):
        admin.call("SELECT admin_reconcile_digital_stock(%s)", (r.choice(products),))

    operations = [op_order_and_pay] * 4 + [op_worker] * 3 + [op_import, op_refund, op_quantity, op_order_pay_refund]
    if admin is not None:
        operations += [op_withdraw, op_cancel, op_resume, op_reconcile]

    def actor(seed):
        r = random.Random(seed)
        for _ in range(int(__import__("os").getenv("DS_STRESS_OPS", "200"))):
            op = r.choice(operations)
            try:
                outcome = op(r)
                key = "business_error" if outcome == "business" else "ok"
                if key == "ok":
                    with lock:
                        stats["ok_by_op"][op.__name__] = stats["ok_by_op"].get(op.__name__, 0) + 1
            except Exception as exc:  # noqa: BLE001
                state = getattr(exc, "sqlstate", None)
                key = {"40P01": "deadlock", "55P03": "lock_timeout"}.get(state)
                # Отказ прав или проверки администратора — НЕ штатный отказ, а ошибка теста/прав.
                denied = state == "42501" or any(text in str(exc) for text in
                                                  ("только администратор", "учётная запись администратора",
                                                   "владельцем схемы", "суперпользовател", "не настроена"))
                if key is None and state in ("P0001", "23505") and not denied:
                    key = "business_error"
                    with lock:
                        reasons = stats.setdefault("refusals_by_op", {}).setdefault(op.__name__, {})
                        text = re.sub(r"\d+", "N", str(exc).split("\n")[0])[:50]
                        reasons[text] = reasons.get(text, 0) + 1
                if key is None:
                    with lock:
                        stats["other"].append(f"{op.__name__}: {type(exc).__name__}: {exc}")
                    continue
            with lock:
                stats[key] += 1

    threads = [threading.Thread(target=actor, args=(rng.random(),)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(300)
        assert not t.is_alive()

    assert stats["deadlock"] == 0 and stats["lock_timeout"] == 0, stats
    assert stats["other"] == [], stats["other"][:5]
    assert stats["ok"] > 100, stats
    if admin is not None:
        for name in ("op_withdraw", "op_cancel", "op_resume", "op_reconcile"):
            assert stats["ok_by_op"].get(name, 0) > 0, (name, stats.get("refusals_by_op", {}).get(name))
    for pid in products:
        assert invariant_holds(pid), (pid, stock(pid))
    bad = q("""
        SELECT f.id, f.status, i.quantity, (SELECT count(*) FROM digital_keys k WHERE k.fulfillment_id = f.id)
        FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id
        WHERE i.product_id = ANY(%s)
          AND (SELECT count(*) FROM digital_keys k WHERE k.fulfillment_id = f.id)
              <> CASE WHEN f.status = 'fulfilled' THEN i.quantity ELSE 0 END""", (products,))
    assert bad == []
    print(f"\nстресс: {stats}")
