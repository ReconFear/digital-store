"""Подтверждённая оплата многопозиционных заказов: остатки, идемпотентность, конкурентность,
атомарность создания заказа.

Тесты работают с ЗАКОММИЧЕННЫМИ данными через отдельные соединения (иначе конкурентность
не проверить). Фикстура make_product удаляет всё созданное после теста.
"""

import threading

import pytest

import database
import payment_service
from commerce.orders import create_order


def _run_parallel(count, target):
    barrier = threading.Barrier(count)
    results = [None] * count
    errors = []

    def worker(index):
        try:
            barrier.wait(timeout=10)
            results[index] = target(index)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    return results


def _stock(db_value, product_id):
    return db_value("SELECT quantity FROM products WHERE id = %s", (product_id,))


def _status(db_value, order_id):
    return db_value("SELECT status FROM orders WHERE id = %s", (order_id,))


def _fulfillments(db_value, order_id):
    return db_value(
        """
        SELECT count(*) FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id
        WHERE i.order_id = %s AND f.status = 'pending' AND f.fulfilled_at IS NULL
        """,
        (order_id,),
    )


# ---------- одна транзакция оплаты ----------

def test_payment_decrements_every_tracked_item_and_creates_fulfillment_tasks(make_product, db_value):
    a = make_product(quantity=10)
    b = make_product(quantity=5)
    files = make_product(quantity=0, product_type="digital_file", inventory_mode="unlimited",
                         fulfillment_type="downloads")
    order = create_order([(a, 3), (b, 5), (files, 2)])

    result = payment_service.apply_verified_payment(order["id"])

    assert result == {"order_id": order["id"], "status": "paid"}
    assert (_stock(db_value, a), _stock(db_value, b), _stock(db_value, files)) == (7, 0, 0)
    assert _status(db_value, order["id"]) == "paid"
    assert db_value("SELECT paid_at IS NOT NULL FROM orders WHERE id = %s", (order["id"],))
    assert _fulfillments(db_value, order["id"]) == 3  # оплачено, но ничего не выдано


def test_repeated_confirmation_is_idempotent(make_product, db_value):
    pid = make_product(quantity=10)
    order = create_order([(pid, 4)])

    first = payment_service.apply_verified_payment(order["id"])
    second = payment_service.apply_verified_payment(order["id"])

    assert first["status"] == "paid"
    assert second == {"status": "already_paid"}
    assert _stock(db_value, pid) == 6
    assert _fulfillments(db_value, order["id"]) == 1


def test_shortage_changes_nothing_but_flags_order_for_a_human(make_product, db_value):
    plenty = make_product(quantity=10)
    scarce = make_product(quantity=5)
    order = create_order([(plenty, 2), (scarce, 5)])
    database_conn_update(f"UPDATE products SET quantity = 3 WHERE id = {scarce}")  # кто-то купил раньше

    result = payment_service.apply_verified_payment(order["id"])

    assert result["error"] == "out_of_stock"
    assert result["status"] == "requires_action"
    assert result["problems"] == [{"product_id": scarce, "reason": "insufficient_stock"}]
    assert (_stock(db_value, plenty), _stock(db_value, scarce)) == (10, 3)  # ни одна позиция не списана
    assert _status(db_value, order["id"]) == "requires_action"
    assert _fulfillments(db_value, order["id"]) == 0

    # Пополнили склад — повторное подтверждение доводит заказ до конца.
    database_conn_update(f"UPDATE products SET quantity = 5 WHERE id = {scarce}")
    assert payment_service.apply_verified_payment(order["id"])["status"] == "paid"
    assert (_stock(db_value, plenty), _stock(db_value, scarce)) == (8, 0)


def test_product_removed_from_sale_after_order_needs_a_human(make_product, db_value):
    pid = make_product(quantity=10)
    order = create_order([(pid, 1)])
    database_conn_update(f"UPDATE products SET is_for_sale = FALSE WHERE id = {pid}")

    result = payment_service.apply_verified_payment(order["id"])

    assert result["problems"] == [{"product_id": pid, "reason": "not_for_sale"}]
    assert _stock(db_value, pid) == 10


@pytest.mark.parametrize("status", ["cancelled", "legacy"])
def test_only_pending_orders_can_be_paid(make_product, db_value, status):
    pid = make_product(quantity=10)
    if status == "legacy":
        # С rev3 перевести заказ в legacy нельзя (переходы статусов); историческая запись
        # создаётся сразу в этом статусе, как это было до появления статусов.
        with database.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("INSERT INTO orders (product_id, price, status) VALUES (%s, 100, 'legacy') RETURNING id",
                               (pid,))
                order = {"id": cursor.fetchone()[0]}
    else:
        order = create_order([(pid, 1)])
        database_conn_update(f"UPDATE orders SET status = '{status}' WHERE id = {order['id']}")

    assert payment_service.apply_verified_payment(order["id"]) == {"error": "invalid_order_status"}
    assert _stock(db_value, pid) == 10


def test_paid_order_cannot_be_reset_to_pending_and_stock_is_not_charged_twice(make_product, db_value):
    pid = make_product(quantity=10)
    order = create_order([(pid, 3)])
    assert payment_service.apply_verified_payment(order["id"])["status"] == "paid"
    paid_at = db_value("SELECT paid_at::text FROM orders WHERE id = %s", (order["id"],))

    with pytest.raises(Exception, match="запрещён"):
        database_conn_update(f"UPDATE orders SET status = 'pending', paid_at = NULL WHERE id = {order['id']}")
    with pytest.raises(Exception, match="paid_at"):
        database_conn_update(f"UPDATE orders SET paid_at = now() + interval '1 hour' WHERE id = {order['id']}")

    assert payment_service.apply_verified_payment(order["id"]) == {"status": "already_paid"}
    assert _stock(db_value, pid) == 7
    assert _status(db_value, order["id"]) == "paid"
    assert db_value("SELECT paid_at::text FROM orders WHERE id = %s", (order["id"],)) == paid_at


def test_unknown_order():
    assert payment_service.apply_verified_payment(2_000_000_000) == {"error": "order_not_found"}


def test_v1_order_is_paid_through_the_same_core(make_product, db_value):
    pid = make_product(quantity=3, price_minor=50000)  # целые рубли -> виден в v1
    order = database.create_order_in_db(pid)

    assert payment_service.apply_verified_payment(order["id"])["status"] == "paid"
    assert _stock(db_value, pid) == 2


def database_conn_update(sql):
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(sql)


# ---------- атомарность создания заказа ----------

def test_failure_while_writing_items_leaves_no_partial_order(make_product, db_value):
    a = make_product()
    b = make_product()
    orders_before = db_value("SELECT count(*) FROM orders")
    items_before = db_value("SELECT count(*) FROM order_items")
    # Сбой базы на второй позиции: временный триггер, который удаляется в finally.
    database_conn_update(f"""
        CREATE FUNCTION pytest_fail_item() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.product_id = {b} THEN RAISE EXCEPTION 'pytest: simulated failure'; END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER pytest_fail_item BEFORE INSERT ON order_items
            FOR EACH ROW EXECUTE FUNCTION pytest_fail_item();
    """)
    try:
        with pytest.raises(Exception, match="simulated failure"):
            create_order([(a, 1), (b, 1)])
    finally:
        database_conn_update("DROP TRIGGER pytest_fail_item ON order_items; DROP FUNCTION pytest_fail_item();")

    assert db_value("SELECT count(*) FROM orders") == orders_before
    assert db_value("SELECT count(*) FROM order_items") == items_before


# ---------- конкурентность ----------

def test_parallel_confirmations_of_one_order_apply_once(make_product, db_value):
    pid = make_product(quantity=10)
    order = create_order([(pid, 3)])

    results = _run_parallel(8, lambda _: payment_service.apply_verified_payment(order["id"]))

    assert sorted(r.get("status") for r in results) == ["already_paid"] * 7 + ["paid"]
    assert _stock(db_value, pid) == 7
    assert _fulfillments(db_value, order["id"]) == 1


def test_orders_competing_for_last_units_never_oversell(make_product, db_value):
    pid = make_product(quantity=3)
    orders = [create_order([(pid, 1)])["id"] for _ in range(8)]  # создание не резервирует

    results = _run_parallel(len(orders), lambda i: payment_service.apply_verified_payment(orders[i]))

    paid = [r for r in results if r.get("status") == "paid"]
    flagged = [r for r in results if r.get("error") == "out_of_stock"]
    assert (len(paid), len(flagged)) == (3, 5)
    assert _stock(db_value, pid) == 0
    assert db_value("SELECT count(*) FROM orders WHERE id = ANY(%s) AND status = 'requires_action'", (orders,)) == 5


def test_opposite_item_order_does_not_deadlock(make_product, db_value):
    a = make_product(quantity=100)
    b = make_product(quantity=100)
    orders = [create_order([(a, 1), (b, 1)] if i % 2 else [(b, 1), (a, 1)])["id"] for i in range(16)]

    results = _run_parallel(len(orders), lambda i: payment_service.apply_verified_payment(orders[i]))

    assert all(r == {"order_id": orders[i], "status": "paid"} for i, r in enumerate(results))
    assert (_stock(db_value, a), _stock(db_value, b)) == (84, 84)


def test_stock_never_goes_negative_under_mixed_load(make_product, db_value):
    a = make_product(quantity=5)
    b = make_product(quantity=5)
    orders = [create_order([(a, 2), (b, 1)])["id"] for _ in range(6)] + \
             [create_order([(b, 3)])["id"] for _ in range(3)]

    _run_parallel(len(orders), lambda i: payment_service.apply_verified_payment(orders[i]))

    stock_a, stock_b = _stock(db_value, a), _stock(db_value, b)
    assert stock_a >= 0 and stock_b >= 0
    # Списано ровно столько, сколько в оплаченных заказах.
    sold = db_value(
        """
        SELECT coalesce(sum(i.quantity), 0) FROM order_items i JOIN orders o ON o.id = i.order_id
        WHERE o.status = 'paid' AND i.product_id = %s
        """,
        (a,),
    )
    assert stock_a == 5 - sold
