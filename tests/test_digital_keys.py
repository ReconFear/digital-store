"""PR5: автоматическая выдача цифровых ключей — функциональные проверки на тестовой базе.

Оплата учитывается настоящей функцией PR3 (payment_record_provider_state), выдача — настоящим
worker (commerce.fulfillment + DigitalKeysHandler), получение — get_delivery (веб-процесс).
ЮKassa не вызывается. Секреты — только тестовые (digital_keys_testkit).
"""

import logging
import uuid

import pytest

import database
import payment_service
from commerce.digital_keys import importer
from commerce.digital_keys.delivery import DeliveryError, delivery_status
from test_migrations import make_schema  # noqa: F401  (фикстура временной схемы)
from commerce.orders import OrderError, create_order
from digital_keys_testkit import (  # noqa: F401  (shop_admin, shop_admin_factory — фикстуры)
    delivery, delivery_keyring, invariant_holds, keyring, make_digital, new_order, one, pay, q, record_success,
    run_worker, shop_admin, shop_admin_factory, start_payment, stock, stock_keys, tasks,
)


def _err(fn, *args, match=None, **kw):
    with pytest.raises(Exception, match=match):
        fn(*args, **kw)


# Административные действия в этих тестах — только через admin_*-функции под временной персональной
# ролью администратора (фикстура shop_admin, нужен DS_TEST_CREATE_ROLES=1). Ядра *_core напрямую не
# вызываются: под ролью, не владеющей таблицами (например, postgres, когда владелец — мигратор), триггер
# fulfillments_guard справедливо отклоняет смену статуса задачи.


# ---------- выдача ----------

def test_one_unit_gets_one_key_and_buyer_sees_it(make_product):
    pid, codes = make_digital(make_product, keys=3)
    order_id, token = new_order([(pid, 1)])
    _, result = pay(order_id)
    assert result["order"]["status"] == "paid"
    assert stock(pid)[0] == 2                        # Q списан при оплате (PR3), не при выдаче

    assert run_worker()["fulfilled"] == 1
    ((_, status, note),) = tasks(order_id)
    assert status == "fulfilled" and note == "Выдано ключей: 1"

    body = delivery(order_id, token)
    (item,) = body["items"]
    assert body["order_status"] == "paid" and item["delivery_status"] == "delivered"
    assert len(item["digital_keys"]) == 1 and item["digital_keys"][0] in codes
    qty, available, assigned, _, awaiting = stock(pid)
    assert (qty, available, assigned, awaiting) == (2, 2, 1, 0)   # выдача Q не трогает
    assert invariant_holds(pid)


def test_three_units_get_three_distinct_keys_and_orders_do_not_overlap(make_product):
    pid, codes = make_digital(make_product, keys=5)
    a, token_a = new_order([(pid, 3)])
    b, token_b = new_order([(pid, 2)])
    pay(a)
    pay(b)
    run_worker()
    keys_a = delivery(a, token_a)["items"][0]["digital_keys"]
    keys_b = delivery(b, token_b)["items"][0]["digital_keys"]
    assert len(keys_a) == 3 and len(set(keys_a)) == 3 and len(keys_b) == 2
    assert not set(keys_a) & set(keys_b)
    assert sorted(keys_a + keys_b) == sorted(codes)
    # Повтор: ключи те же, порядок стабилен.
    assert delivery(a, token_a)["items"][0]["digital_keys"] == keys_a


def test_repeat_worker_run_assigns_nothing_new(make_product):
    pid, _ = make_digital(make_product, keys=2)
    order_id, _ = new_order([(pid, 1)])
    pay(order_id)
    run_worker()
    assert run_worker() == {"fulfilled": 0, "in_progress": 0, "requires_action": 0, "retry": 0, "failed": 0}
    assert one("SELECT count(*) FROM digital_keys WHERE product_id = %s AND status = 'assigned'", (pid,)) == 1


def test_unpaid_order_gets_nothing(make_product):
    pid, _ = make_digital(make_product, keys=2)
    order_id, token = new_order([(pid, 1)])
    run_worker()
    body = delivery(order_id, token)
    assert body["order_status"] == "pending"
    assert body["items"][0]["delivery_status"] == "awaiting_payment" and "digital_keys" not in body["items"][0]
    assert one("SELECT count(*) FROM digital_keys WHERE product_id = %s AND status = 'assigned'", (pid,)) == 0


def test_without_module_task_stays_pending(make_product):
    from commerce import fulfillment
    pid, _ = make_digital(make_product, keys=1)
    order_id, token = new_order([(pid, 1)])
    pay(order_id)
    assert fulfillment.process_pending_fulfillments()["fulfilled"] == 0
    assert tasks(order_id)[0][1] == "pending"
    assert delivery(order_id, token)["items"][0]["delivery_status"] == "processing"


def test_mixed_order_manual_item_is_reported_as_manual(make_product):
    pid, _ = make_digital(make_product, keys=1)
    service = make_product(quantity=0, product_type="service", inventory_mode="unlimited")
    order_id, token = new_order([(pid, 1), (service, 1)])
    pay(order_id)
    run_worker()
    statuses = [(i["fulfillment_type"], i["delivery_status"]) for i in delivery(order_id, token)["items"]]
    assert statuses == [("digital_keys", "delivered"), ("manual", "manual")]


# ---------- R1: продаваемый остаток и проверка при оплате ----------

def test_sellable_stock_is_backed_by_keys(make_product, db_value):
    pid, _ = make_digital(make_product, keys=3)
    assert db_value("SELECT sellable_quantity FROM product_sellable_stock WHERE product_id = %s", (pid,)) == 3
    from commerce import catalog
    assert catalog.get_public_product(pid)["stock_quantity"] == 3

    # Ручное уменьшение Q — без проверки; S = min(Q, A − W).
    q("UPDATE products SET quantity = 1 WHERE id = %s", (pid,))
    assert db_value("SELECT sellable_quantity FROM product_sellable_stock WHERE product_id = %s", (pid,)) == 1
    with pytest.raises(OrderError) as error:
        create_order([(pid, 2)])
    assert error.value.code == "insufficient_stock"


def test_raising_quantity_above_keys_is_refused(make_product):
    pid, _ = make_digital(make_product, keys=2)
    _err(q, "UPDATE products SET quantity = 3 WHERE id = %s", (pid,), match="больше обеспеченного")
    q("UPDATE products SET quantity = 1 WHERE id = %s", (pid,))
    q("UPDATE products SET quantity = 2 WHERE id = %s", (pid,))   # обратно до A − W — можно


def test_switching_to_automatic_delivery_requires_keys(make_product):
    manual = make_product(quantity=4, product_type="digital_key", fulfillment_type="manual")
    _err(q, "UPDATE products SET fulfillment_type = 'digital_keys' WHERE id = %s", (manual,),
         match="больше обеспеченного")
    stock_keys(manual, 4)                         # ключи для manual-товара остаток не меняют
    assert one("SELECT quantity FROM products WHERE id = %s", (manual,)) == 4
    q("UPDATE products SET fulfillment_type = 'digital_keys' WHERE id = %s", (manual,))
    assert invariant_holds(manual)


def test_legacy_product_without_keys_cannot_be_paid_and_goes_requires_action(make_product, shop_admin):
    """Старый товар digital_keys с Q > 0 без ключей (до PR5): не продаётся (S = 0). Если заказ создан
    до появления проверки и оплачен — деньги учтены, заказ requires_action, остаток не списан."""
    pid = make_product(quantity=5, product_type="digital_key", fulfillment_type="digital_keys")
    with pytest.raises(OrderError):
        create_order([(pid, 1)])
    # Заказ, созданный в обход проверки остатка (как до PR5): позиция вставляется напрямую.
    order_id = _legacy_order(pid, 1)
    _, result = pay(order_id)
    assert result["order"]["error"] == "out_of_stock"
    assert result["order"]["problems"] == [{"product_id": pid, "reason": "insufficient_stock"}]
    assert one("SELECT status FROM orders WHERE id = %s", (order_id,)) == "requires_action"
    assert one("SELECT quantity FROM products WHERE id = %s", (pid,)) == 5
    assert tasks(order_id) == []
    assert one("SELECT count(*) FROM money_operations WHERE order_id = %s", (order_id,)) == 1

    # Административная процедура: загрузить ключи, повторно провести оплаченный заказ.
    stock_keys(pid, 1)
    assert one("SELECT quantity FROM products WHERE id = %s", (pid,)) == 5   # завышенный Q не растёт
    assert one("SELECT admin_retry_paid_order(%s)->>'status'", (order_id,)) == "paid"
    run_worker()
    assert tasks(order_id)[0][1] == "fulfilled"
    # Сверка: Q := min(Q, A − W) = 0.
    assert shop_admin.call("SELECT admin_reconcile_digital_stock(%s)", (pid,))["new_quantity"] == 0
    assert invariant_holds(pid)


def _legacy_order(pid, quantity):
    import secrets
    token = secrets.token_urlsafe(32)
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT name, price_minor FROM products WHERE id = %s", (pid,))
            name, price = cursor.fetchone()
            cursor.execute("INSERT INTO orders (status, currency, total_minor) VALUES ('pending', 'RUB', %s) "
                           "RETURNING id", (price * quantity,))
            order_id = cursor.fetchone()[0]
            cursor.execute(
                "INSERT INTO order_items (order_id, product_id, product_name, product_type, fulfillment_type, "
                "quantity, unit_price_minor, currency, line_total_minor) "
                "VALUES (%s, %s, %s, 'digital_key', 'digital_keys', %s, %s, 'RUB', %s)",
                (order_id, pid, name, quantity, price, price * quantity))
    return order_id


def test_two_orders_for_last_key_second_payment_goes_requires_action(make_product):
    """Проверка до оплаты — подсказка: оба заказа созданы при S = 1, оплачены оба. Второй честно
    уходит в requires_action, ключ один, двойной выдачи нет."""
    pid, _ = make_digital(make_product, keys=1)
    a, _ = new_order([(pid, 1)])
    b, _ = new_order([(pid, 1)])
    assert pay(a)[1]["order"]["status"] == "paid"
    assert pay(b)[1]["order"]["error"] == "out_of_stock"
    run_worker()
    assert one("SELECT count(*) FROM digital_keys WHERE product_id = %s AND status = 'assigned'", (pid,)) == 1
    assert invariant_holds(pid)


# ---------- нехватка после оплаты, возобновление, отмена ----------

def _shortage_after_payment(make_product, keys=2, need=2):
    """Оплата прошла при достаточном запасе, затем свободные ключи изъяты в обход учёта остатка
    (имитация: строки ключей помечены изъятыми напрямую). Worker видит нехватку."""
    pid, _ = make_digital(make_product, keys=keys)
    order_id, token = new_order([(pid, need)])
    pay(order_id)
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("UPDATE digital_keys SET status = 'withdrawn', withdrawn_at = now(), "
                           "withdraw_reason = 'pytest' WHERE product_id = %s AND status = 'available'", (pid,))
    return pid, order_id, token


def test_shortage_after_payment_goes_requires_action_without_partial_assignment(make_product):
    pid, order_id, token = _shortage_after_payment(make_product)
    summary = run_worker()
    assert summary["requires_action"] == 1
    ((fid, status, note),) = tasks(order_id)
    assert status == "requires_action" and note == "Не хватает ключей: нужно 2, свободно 0"
    assert one("SELECT count(*) FROM digital_keys WHERE fulfillment_id = %s", (fid,)) == 0
    assert delivery(order_id, token)["items"][0]["delivery_status"] == "delayed"
    assert one("SELECT count(*) FROM fulfillment_issues WHERE fulfillment_id = %s", (fid,)) == 1
    assert one("SELECT count(*) FROM audit_log WHERE action = 'digital_keys.shortage' AND order_id = %s",
               (order_id,)) == 1
    # Worker сам задачу не возобновляет.
    run_worker()
    assert tasks(order_id)[0][1] == "requires_action"


def test_resume_refused_before_import_and_works_after(make_product, shop_admin):
    pid, order_id, token = _shortage_after_payment(make_product, keys=1, need=1)
    run_worker()
    fid = tasks(order_id)[0][0]
    _err(shop_admin.resume, fid, "пополнили", match="меньше нужного")
    stock_keys(pid, 1)
    assert shop_admin.resume(fid, "пополнили")["status"] == "pending"
    run_worker()
    assert tasks(order_id)[0][1] == "fulfilled"
    assert len(delivery(order_id, token)["items"][0]["digital_keys"]) == 1
    assert one("SELECT details->>'by' FROM audit_log WHERE action = 'fulfillment.resumed' AND order_id = %s",
               (order_id,)) == shop_admin.name


def test_cancel_keeps_quantity_and_money(make_product, shop_admin):
    pid, order_id, token = _shortage_after_payment(make_product, keys=1, need=1)
    run_worker()
    fid = tasks(order_id)[0][0]
    qty_before = one("SELECT quantity FROM products WHERE id = %s", (pid,))
    money_before = q("SELECT kind, amount_minor FROM money_operations WHERE order_id = %s", (order_id,))
    assert shop_admin.cancel(fid, "возврат клиенту")["status"] == "cancelled"
    assert one("SELECT quantity FROM products WHERE id = %s", (pid,)) == qty_before
    assert q("SELECT kind, amount_minor FROM money_operations WHERE order_id = %s", (order_id,)) == money_before
    assert delivery(order_id, token)["items"][0]["delivery_status"] == "cancelled"
    # Отменённая задача конечна: ни worker, ни возобновление её не вернут.
    _err(shop_admin.resume, fid, "x", match="requires_action или failed")
    _err(q, "UPDATE fulfillments SET status = 'pending' WHERE id = %s", (fid,), match="конечное")


def test_fulfilled_task_cannot_be_cancelled_or_reopened(make_product, shop_admin):
    pid, _ = make_digital(make_product, keys=1)
    order_id, _ = new_order([(pid, 1)])
    pay(order_id)
    run_worker()
    fid = tasks(order_id)[0][0]
    _err(shop_admin.cancel, fid, "x", match="выданное не отзывается")
    _err(q, "UPDATE fulfillments SET status = 'pending' WHERE id = %s", (fid,), match="конечное")


# ---------- R2: остановка выдачи при запросе возврата ----------

def test_refund_request_holds_delivery_until_explicit_release(make_product, shop_admin):
    pid, _ = make_digital(make_product, keys=1)
    order_id, token = new_order([(pid, 1)])
    payment_id, _ = pay(order_id)
    one("SELECT request_refund(%s, 100, 'клиент передумал')", (payment_id,))

    assert run_worker()["requires_action"] == 1
    ((fid, status, note),) = tasks(order_id)
    assert status == "requires_action" and "возврат" in note
    assert one("SELECT count(*) FROM digital_keys WHERE product_id = %s AND status = 'assigned'", (pid,)) == 0
    assert delivery(order_id, token)["items"][0]["delivery_status"] == "delayed"

    # Обычное возобновление не снимает остановку.
    shop_admin.resume(fid, "проверили")
    run_worker()
    assert tasks(order_id)[0][1] == "requires_action"
    # Явное снятие остановки администратором.
    shop_admin.resume(fid, "возврат отменён", release_refund_hold=True)
    run_worker()
    assert tasks(order_id)[0][1] == "fulfilled"


def test_canceled_refund_does_not_hold(make_product):
    pid, _ = make_digital(make_product, keys=1)
    order_id, _ = new_order([(pid, 1)])
    payment_id, _ = pay(order_id)
    refund_id = one("SELECT request_refund(%s, 100, 'ошибка')", (payment_id,))
    q("UPDATE refunds SET status = 'canceled' WHERE id = %s", (refund_id,))
    run_worker()
    assert tasks(order_id)[0][1] == "fulfilled"


def test_refund_on_surplus_payment_does_not_hold(make_product):
    """Возврат лишнего (повторного) платежа не останавливает выдачу: учитывается только платёж,
    который действительно оплатил заказ (status = succeeded)."""
    pid, _ = make_digital(make_product, keys=1)
    order_id, _ = new_order([(pid, 1)])
    # Первая попытка «потерялась» (PR3: requires_action / unresolved_attempt), покупатель оплатил
    # второй, затем первая нашлась у провайдера оплаченной.
    first = start_payment(order_id)
    q("UPDATE payments SET status = 'requires_action', status_reason = 'unresolved_attempt' WHERE id = %s",
      (first[0],))
    second = start_payment(order_id)
    assert record_success(*second)["order"]["status"] == "paid"
    result = record_success(*first)               # duplicate_payment -> requires_action + refund
    surplus_id = first[0]
    assert result["reason"] == "duplicate_payment"
    assert one("SELECT count(*) FROM refunds WHERE payment_id = %s", (surplus_id,)) == 1
    run_worker()
    assert tasks(order_id)[0][1] == "fulfilled"


# ---------- изъятие и сверка остатка ----------

def test_withdraw_respects_invariant(make_product, shop_admin):
    pid, _ = make_digital(make_product, keys=2)
    key = one("SELECT public_id::text FROM digital_keys WHERE product_id = %s ORDER BY id LIMIT 1", (pid,))
    withdraw = "SELECT admin_withdraw_digital_key(%s, %s)"
    _err(shop_admin.call, withdraw, (key, "утёк"), match="сначала уменьшите остаток")
    q("UPDATE products SET quantity = 1 WHERE id = %s", (pid,))
    assert shop_admin.call(withdraw, (key, "утёк"))["status"] == "withdrawn"
    assert stock(pid)[:4] == (1, 1, 0, 1) and invariant_holds(pid)
    _err(shop_admin.call, withdraw, (key, "ещё раз"), match="только свободный")


def test_withdraw_of_assigned_key_is_refused(make_product, shop_admin):
    pid, _ = make_digital(make_product, keys=1)
    order_id, _ = new_order([(pid, 1)])
    pay(order_id)
    run_worker()
    key = one("SELECT public_id::text FROM digital_keys WHERE product_id = %s", (pid,))
    _err(shop_admin.call, "SELECT admin_withdraw_digital_key(%s, 'x')", (key,), match="только свободный")


def test_reconcile_only_decreases(make_product, shop_admin):
    pid, _ = make_digital(make_product, keys=3)
    reconcile = "SELECT admin_reconcile_digital_stock(%s)"
    assert shop_admin.call(reconcile, (pid,))["new_quantity"] == 3
    q("UPDATE products SET quantity = 1 WHERE id = %s", (pid,))
    assert shop_admin.call(reconcile, (pid,))["new_quantity"] == 1


# ---------- импорт ----------

def test_import_rejects_whole_batch_with_duplicates_unless_skip(make_product):
    pid, codes = make_digital(make_product, keys=2)
    fresh = [f"NEW-{uuid.uuid4().hex}"]
    _, report = stock_keys(pid, codes=fresh + [codes[0]] + fresh)
    assert report["status"] == "rejected"
    assert report["duplicates_in_batch_lines"] == [3] and report["duplicates_existing_lines"] == [2]
    assert stock(pid)[1] == 2

    _, report = stock_keys(pid, codes=fresh + [codes[0]] + fresh, skip_duplicates=True)
    assert report["status"] == "imported" and report["inserted"] == 1 and report["quantity_added"] == 1
    assert stock(pid)[:2] == (3, 3)


def test_duplicates_are_global_across_products_and_case_sensitive(make_product):
    a, codes = make_digital(make_product, keys=1)
    b, _ = make_digital(make_product)
    assert stock_keys(b, codes=codes)[1]["status"] == "rejected"
    assert stock_keys(b, codes=[codes[0].lower()])[1]["status"] == "imported"


def test_import_report_and_database_contain_no_plaintext(make_product):
    pid, codes = make_digital(make_product, keys=3)
    dump = str(q("SELECT * FROM digital_keys WHERE product_id = %s", (pid,))) + str(
        q("SELECT * FROM digital_key_batches WHERE product_id = %s", (pid,))) + str(
        q("SELECT details::text FROM audit_log WHERE (details->>'product_id')::int = %s", (pid,)))
    for code in codes:
        assert code not in dump and code.encode().hex() not in dump
    assert one("SELECT imported_by FROM digital_key_batches WHERE product_id = %s", (pid,)) == "pytest"


def test_import_needs_registered_keys(make_product):
    pid, _ = make_digital(make_product)
    batch = importer.prepare_batch(keyring(), pid, [(1, "X-1")])
    batch.enc_key_ids = ["zz9"]
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            with pytest.raises(Exception, match="не зарегистрирован"):
                importer.submit(cursor, batch, "pytest", core_actor="pytest")


def test_import_only_for_digital_key_products(make_product):
    other = make_product(quantity=1)
    with pytest.raises(Exception, match="digital_key"):
        stock_keys(other, 1)


def test_import_for_manual_product_does_not_change_quantity(make_product):
    pid = make_product(quantity=7, product_type="digital_key", fulfillment_type="manual")
    _, report = stock_keys(pid, 3)
    assert report["quantity_added"] == 0 and one("SELECT quantity FROM products WHERE id = %s", (pid,)) == 7


def test_reading_codes_normalizes_and_rejects_bad_lines():
    codes = importer.read_codes(["﻿AAA-1 \r\n", "", "  BbB-2\t", "   "])
    assert codes == [(1, "AAA-1"), (3, "BbB-2")]
    with pytest.raises(importer.ImportProblem, match="строка 2"):
        importer.read_codes(["OK", "BAD\x00KEY"])
    with pytest.raises(importer.ImportProblem, match="нет ключей"):
        importer.read_codes(["", " "])


# ---------- целостность в базе ----------

def test_key_rows_are_immutable_and_not_deletable(make_product):
    pid, _ = make_digital(make_product, keys=1)
    kid = one("SELECT id FROM digital_keys WHERE product_id = %s", (pid,))
    _err(q, "UPDATE digital_keys SET ciphertext = ciphertext || '\\x00'::bytea WHERE id = %s", (kid,),
         match="неизменяемо")
    _err(q, "DELETE FROM digital_keys WHERE id = %s", (kid,), match="не удаляются")
    _err(q, "TRUNCATE digital_keys", match="запрещён")
    _err(q, "UPDATE digital_key_batches SET source_label = 'x' WHERE product_id = %s", (pid,), match="запрещён")
    _err(q, "UPDATE encryption_key_registry SET registered_by = 'x' WHERE key_id = 't1'", match="запрещён")


def test_keys_cannot_be_assigned_to_unpaid_or_foreign_tasks(make_product):
    pid, _ = make_digital(make_product, keys=2)
    other, _ = make_digital(make_product, keys=1)
    unpaid, _ = new_order([(pid, 1)])
    paid, _ = new_order([(other, 1)])
    pay(paid)
    fid, = tasks(paid)[0][:1]
    item = one("SELECT order_item_id FROM fulfillments WHERE id = %s", (fid,))
    kid = one("SELECT id FROM digital_keys WHERE product_id = %s ORDER BY id LIMIT 1", (pid,))
    _err(q, "UPDATE digital_keys SET status = 'assigned', fulfillment_id = %s, order_item_id = %s, assigned_at = now() "
            "WHERE id = %s", (fid, item, kid), match="нельзя назначить")
    assert unpaid


def test_task_cannot_get_more_keys_than_quantity(make_product):
    pid, _ = make_digital(make_product, keys=3)
    order_id, _ = new_order([(pid, 1)])
    pay(order_id)
    fid = tasks(order_id)[0][0]
    item = one("SELECT order_item_id FROM fulfillments WHERE id = %s", (fid,))
    _err(q, "UPDATE digital_keys SET status = 'assigned', fulfillment_id = %s, order_item_id = %s, assigned_at = now() "
            "WHERE product_id = %s AND status = 'available'", (fid, item, pid), match="больше 1")


def test_fulfilled_requires_exactly_quantity_keys(make_product):
    pid, _ = make_digital(make_product, keys=2)
    order_id, _ = new_order([(pid, 2)])
    pay(order_id)
    fid = tasks(order_id)[0][0]
    _err(q, "UPDATE fulfillments SET status = 'fulfilled' WHERE id = %s", (fid,), match="назначены не все")


def test_assign_function_refuses_wrong_states(make_product):
    pid, _ = make_digital(make_product, keys=1)
    order_id, _ = new_order([(pid, 1)])
    pay(order_id)
    fid = tasks(order_id)[0][0]
    manual = make_product(quantity=1)
    m_order, _ = new_order([(manual, 1)])
    pay(m_order)
    _err(one, "SELECT digital_keys_assign(%s)", (tasks(m_order)[0][0],), match="не является выдачей")
    assert one("SELECT digital_keys_assign(%s)->>'result'", (fid,)) == "assigned"  # отдельная транзакция, COMMIT
    # Ключ назначен, задача ещё pending (имитация: модуль назначил, а результат не записан).
    assert one("SELECT digital_keys_assign(%s)->>'result'", (fid,)) == "already_assigned"
    run_worker()
    assert tasks(order_id)[0][1] == "fulfilled"
    assert one("SELECT digital_keys_assign(%s)->>'result'", (fid,)) == "not_pending"


# ---------- получение ключей ----------

def test_wrong_or_foreign_token_is_the_same_404(make_product):
    pid, _ = make_digital(make_product, keys=2)
    a, token_a = new_order([(pid, 1)])
    b, token_b = new_order([(pid, 1)])
    pay(a)
    pay(b)
    run_worker()
    for order_id, token in ((a, token_b), (a, "x" * 43), (a, ""), (a, None), (999999999, token_a), (0, token_a),
                            (2 ** 31, token_a)):
        with pytest.raises(DeliveryError) as error:
            delivery(order_id, token)
        assert (error.value.code, error.value.http_status) == ("order_not_found", 404)


def test_each_reveal_is_audited_without_values(make_product):
    pid, codes = make_digital(make_product, keys=1)
    order_id, token = new_order([(pid, 1)])
    pay(order_id)
    run_worker()
    delivery(order_id, token)
    delivery(order_id, token)
    rows = q("SELECT details::text FROM audit_log WHERE action = 'digital_keys.revealed' AND order_id = %s", (order_id,))
    assert len(rows) == 2 and all(codes[0] not in r[0] for r in rows)


def test_tampered_or_moved_ciphertext_gives_503_and_no_audit(make_product, caplog):
    pid, codes = make_digital(make_product, keys=2)
    order_id, token = new_order([(pid, 1)])
    pay(order_id)
    run_worker()
    assigned = one("SELECT id FROM digital_keys WHERE product_id = %s AND status = 'assigned'", (pid,))
    free = one("SELECT id FROM digital_keys WHERE product_id = %s AND status = 'available'", (pid,))
    # Подмена шифротекста назначенного ключа шифротекстом другой записи (обход триггера — только для теста).
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("ALTER TABLE digital_keys DISABLE TRIGGER digital_keys_guard")
            cursor.execute("UPDATE digital_keys d SET nonce = s.nonce, ciphertext = s.ciphertext "
                           "FROM digital_keys s WHERE d.id = %s AND s.id = %s", (assigned, free))
            cursor.execute("ALTER TABLE digital_keys ENABLE TRIGGER digital_keys_guard")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(DeliveryError) as error:
        delivery(order_id, token)
    assert (error.value.code, error.value.http_status) == ("delivery_unavailable", 503)
    assert one("SELECT count(*) FROM audit_log WHERE action = 'digital_keys.revealed' AND order_id = %s",
               (order_id,)) == 0
    assert all(code not in caplog.text for code in codes)


def test_missing_or_wrong_keyring_gives_503(make_product, monkeypatch):
    from commerce.digital_keys import delivery as module
    from commerce.digital_keys.crypto import Keyring
    pid, _ = make_digital(make_product, keys=1)
    order_id, token = new_order([(pid, 1)])
    pay(order_id)
    run_worker()
    wrong = Keyring.from_env({"DIGITAL_KEYS_ENCRYPTION_KEYS": "t1:" + "A" * 43 + "="})
    for loader in (lambda: Keyring.from_env({}), lambda: wrong):
        with pytest.raises(DeliveryError) as error:
            module.get_delivery(order_id, token, keyring_loader=loader)
        assert error.value.http_status == 503


def test_order_without_keys_to_show_does_not_need_keyring(make_product):
    from commerce.digital_keys import delivery as module
    pid, _ = make_digital(make_product, keys=1)
    order_id, token = new_order([(pid, 1)])

    def no_keyring():
        raise AssertionError("секреты не нужны")

    assert module.get_delivery(order_id, token, keyring_loader=no_keyring)["items"][0]["delivery_status"] == \
        "awaiting_payment"


def test_rotation_old_and_new_versions_are_both_readable(make_product):
    pid, old_codes = make_digital(make_product, keys=1)            # версия t1
    new_codes, _ = stock_keys(pid, 1, active="t2")                 # версия t2
    assert sorted(r[0] for r in q("SELECT enc_key_id FROM digital_keys WHERE product_id = %s", (pid,))) == ["t1", "t2"]
    order_id, token = new_order([(pid, 2)])
    pay(order_id)
    run_worker()
    assert sorted(delivery(order_id, token)["items"][0]["digital_keys"]) == sorted(old_codes + new_codes)


@pytest.mark.parametrize("order_status, ftype, task, expected", [
    ("pending", "digital_keys", None, "awaiting_payment"),
    ("requires_action", "digital_keys", None, "delayed"),
    ("cancelled", "digital_keys", None, "not_applicable"),
    ("legacy", "manual", None, "not_applicable"),
    ("paid", "digital_keys", None, "not_applicable"),
    ("paid", "manual", "pending", "manual"),
    ("paid", "downloads", "pending", "manual"),
    ("paid", "digital_keys", "pending", "processing"),
    ("paid", "digital_keys", "in_progress", "processing"),
    ("paid", "digital_keys", "fulfilled", "delivered"),
    ("paid", "digital_keys", "requires_action", "delayed"),
    ("paid", "digital_keys", "failed", "delayed"),
    ("paid", "digital_keys", "cancelled", "cancelled"),
    ("paid", "manual", "cancelled", "cancelled"),
])
def test_delivery_status_mapping(order_status, ftype, task, expected):
    assert delivery_status(order_status, ftype, task) == expected


def test_v1_catalog_shows_sellable_quantity(make_product):
    import database as db
    pid = make_product(quantity=5, product_type="digital_key", fulfillment_type="digital_keys")
    assert db.get_public_product_from_db(pid)["quantity"] == 0
    assert db.create_order_in_db(pid) == {"error": "unavailable"}
    stock_keys(pid, 2)
    assert db.get_public_product_from_db(pid)["quantity"] == 2


def test_payment_service_wrapper_respects_r1(make_product):
    pid = make_product(quantity=3, product_type="digital_key", fulfillment_type="digital_keys")
    order_id = _legacy_order(pid, 1)
    assert payment_service.apply_verified_payment(order_id)["error"] == "out_of_stock"
    assert delivery_keyring()   # тестовая связка ключей для веб-процесса создаётся без секрета отпечатков


def test_registry_allows_only_retiring_once():
    from digital_keys_testkit import register_test_keys
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            register_test_keys(cursor)
    _err(q, "DELETE FROM encryption_key_registry WHERE key_id = 't1'", match="запрещён")
    _err(one, "SELECT encryption_key_register_core('encryption', 't1', repeat('00', 16), 'pytest')",
         match="ДРУГИМ проверочным значением")


def test_delivery_checks_registry_before_decrypting(make_product):
    """Расшифровка выполняется только после сверки проверочных значений с реестром: секрет, не
    совпавший с зарегистрированным (здесь — подменённое значение), даёт 503 даже если бы расшифровал."""
    from commerce.digital_keys import delivery as module
    pid, _ = make_digital(make_product, keys=1)
    order_id, token = new_order([(pid, 1)])
    pay(order_id)
    run_worker()

    def mismatched():
        ring = delivery_keyring()
        ring.check_values = lambda: {"encryption:t1": "00" * 16, "encryption:t2": "00" * 16}
        return ring

    with pytest.raises(DeliveryError) as error:
        module.get_delivery(order_id, token, keyring_loader=mismatched)
    assert error.value.http_status == 503
    assert module.get_delivery(order_id, token, keyring_loader=delivery_keyring)["items"][0]["digital_keys"]


# ---------- ревью PR5: разделение строк файла импорта ----------

@pytest.mark.parametrize("separator, name", [(" ", "U+2028"), (" ", "U+2029"), ("\x85", "U+0085"),
                                             ("\x0b", "VT"), ("\x0c", "FF"), ("\x1c", "FS"), ("\x1e", "RS")])
def test_import_file_rejects_hidden_line_breaks_inside_a_code(tmp_path, separator, name):
    """Символ, который str.splitlines() считает разрывом строки, внутри кода не делит его на два
    ключа: строка отклоняется, партия не загружается."""
    path = tmp_path / "keys.txt"
    path.write_bytes(f"GOOD-1\r\nAAA{separator}BBB\r\nGOOD-2\r\n".encode("utf-8"))
    with pytest.raises(importer.ImportProblem) as error:
        importer.read_codes_file(path)
    assert "строка 2" in str(error.value) and "AAA" not in str(error.value), name


@pytest.mark.parametrize("raw", [b"A-1\r\nB-2\r\nC-3\r\n", b"A-1\nB-2\nC-3", b"A-1\rB-2\rC-3\r",
                                 b"\xef\xbb\xbfA-1\r\nB-2\r\n\r\nC-3\r\n"])
def test_import_file_line_endings(tmp_path, raw):
    path = tmp_path / "keys.txt"
    path.write_bytes(raw)
    assert [code for _, code in importer.read_codes_file(path)] == ["A-1", "B-2", "C-3"]


def test_import_file_line_numbers_follow_the_file(tmp_path):
    path = tmp_path / "keys.txt"
    path.write_bytes(b"A-1\r\n\r\nB-2\r\n")
    assert importer.read_codes_file(path) == [(1, "A-1"), (3, "B-2")]


# ---------- ревью PR5: одна версия секрета отпечатков на склад ----------

@pytest.fixture
def empty_store(make_schema):
    """Отдельная схема с пустым складом (проверка версии отпечатков — по всему складу)."""
    import migrate
    from test_migrations import _quiet
    conn, schema = make_schema()
    migrate.apply_migrations(conn, log=_quiet)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO products (name, price_minor, quantity, is_for_sale, product_type, inventory_mode, "
                    "fulfillment_type) VALUES ('A', 100, 0, TRUE, 'digital_key', 'tracked', 'digital_keys'), "
                    "('B', 100, 0, TRUE, 'digital_key', 'tracked', 'digital_keys') RETURNING id")
        products = [row[0] for row in cur.fetchall()]
    conn.commit()
    return conn, products


def _schema_import(conn, ring, product_id, codes):
    for name, value in ring.check_values().items():
        purpose, key_id = name.split(":", 1)
        with conn.cursor() as cur:
            cur.execute("SELECT encryption_key_register_core(%s, %s, %s, 'pytest')", (purpose, key_id, value))
    batch = importer.prepare_batch(ring, product_id, list(enumerate(codes, start=1)))
    try:
        with conn.cursor() as cur:
            report = importer.submit(cur, batch, "pytest", core_actor="pytest")
        conn.commit()
        return report
    except Exception:
        conn.rollback()
        raise


def _ring(fingerprint, active="t1"):
    from digital_keys_testkit import TEST_ENV, _secret
    from commerce.digital_keys.crypto import Keyring
    env = dict(TEST_ENV, DIGITAL_KEYS_ACTIVE_KEY=active)
    if fingerprint != "tf1":
        env["DIGITAL_KEYS_FINGERPRINT_KEY"] = f"{fingerprint}:{_secret('fp-' + fingerprint)}"
    return Keyring.from_env(env, need_active=True, need_fingerprint=True)


def test_first_fingerprint_version_on_empty_store_is_allowed_then_fixed(empty_store):
    conn, (a, b) = empty_store
    # Пустой склад: первая версия может быть любой зарегистрированной (здесь tf2).
    assert _schema_import(conn, _ring("tf2"), a, ["CODE-1"])["status"] == "imported"
    # Повтор того же кода под тем же секретом — дубликат.
    assert _schema_import(conn, _ring("tf2"), b, ["CODE-1"])["status"] == "rejected"
    # Другая версия HMAC при непустом складе — отказ базы; под ней тот же код прошёл бы как новый.
    with pytest.raises(Exception, match="отпечатками версии tf2"):
        _schema_import(conn, _ring("tf1"), b, ["CODE-1"])
    with pytest.raises(Exception, match="отпечатками версии tf2"):
        _schema_import(conn, _ring("tf1"), b, ["CODE-NEW"])
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), count(DISTINCT fingerprint_key_id) FROM digital_keys")
        assert cur.fetchone() == (1, 1)
    conn.rollback()


def test_withdrawn_keys_still_pin_the_fingerprint_version(empty_store):
    conn, (a, _) = empty_store
    _schema_import(conn, _ring("tf1"), a, ["W-1"])
    with conn.cursor() as cur:
        cur.execute("UPDATE products SET quantity = 0 WHERE id = %s", (a,))
        cur.execute("SELECT digital_keys_withdraw_core(public_id, 'pytest', 'pytest') FROM digital_keys")
    conn.commit()
    with pytest.raises(Exception, match="отпечатками версии tf1"):
        _schema_import(conn, _ring("tf2"), a, ["W-1"])


def test_encryption_version_changes_independently_of_hmac(empty_store):
    conn, (a, _) = empty_store
    assert _schema_import(conn, _ring("tf1", active="t1"), a, ["E-1"])["status"] == "imported"
    assert _schema_import(conn, _ring("tf1", active="t2"), a, ["E-2"])["status"] == "imported"
    # Повтор кода, зашифрованного другой версией AES, обнаруживается: отпечаток от шифрования не зависит.
    assert _schema_import(conn, _ring("tf1", active="t2"), a, ["E-1"])["status"] == "rejected"
    with conn.cursor() as cur:
        cur.execute("SELECT enc_key_id, fingerprint_key_id FROM digital_keys ORDER BY id")
        assert cur.fetchall() == [("t1", "tf1"), ("t2", "tf1")]
    conn.rollback()


# ---------- административные действия: посторонний и администратор ----------

ADMIN_ACTIONS = [
    ("SELECT admin_resume_fulfillment(%s, 'x', FALSE)", "admin_resume_fulfillment(bigint, text, boolean)"),
    ("SELECT admin_cancel_fulfillment(%s, 'x')", "admin_cancel_fulfillment(bigint, text)"),
]


@pytest.mark.parametrize("sql, signature", ADMIN_ACTIONS)
def test_outsider_cannot_resume_or_cancel_even_with_execute(make_product, shop_admin_factory, sql, signature):
    """Посторонняя роль без членства в группе администраторов: без EXECUTE — отказ прав PostgreSQL;
    с явно выданным EXECUTE — отказ shop_require_admin. Задача не меняется."""
    pid, order_id, _ = _shortage_after_payment(make_product, keys=1, need=1)
    run_worker()
    fid = tasks(order_id)[0][0]
    stock_keys(pid, 1)
    plain = shop_admin_factory("outsider", member=False)
    with pytest.raises(Exception) as error:
        plain.call(sql, (fid,))
    assert getattr(error.value, "sqlstate", None) == "42501"
    with_execute = shop_admin_factory("rogue", member=False, extra_execute=(signature,))
    _err(with_execute.call, sql, (fid,), match="учётная запись администратора")
    assert tasks(order_id)[0][1] == "requires_action"
    admin = shop_admin_factory("admin")
    expected = "pending" if "resume" in sql else "cancelled"
    assert admin.call(sql, (fid,))["status"] == expected
    assert one("SELECT details->>'by' FROM audit_log WHERE order_id = %s AND action LIKE 'fulfillment.%%' "
               "ORDER BY id DESC LIMIT 1", (order_id,)) == admin.name


def test_direct_status_change_without_admin_function_is_refused_for_non_owner(make_product, shop_admin):
    """Администратор не может обойти функцию прямым UPDATE (у группы нет UPDATE на fulfillments), а
    триггер отклоняет выход из requires_action у любой роли, кроме владельца таблицы."""
    _, order_id, _ = _shortage_after_payment(make_product, keys=1, need=1)
    run_worker()
    fid = tasks(order_id)[0][0]
    with pytest.raises(Exception) as error:
        shop_admin.call("UPDATE fulfillments SET status = 'pending' WHERE id = %s", (fid,))
    assert getattr(error.value, "sqlstate", None) == "42501"
