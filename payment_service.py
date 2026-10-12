"""Применение подтверждённой оплаты к заказу.

ВНИМАНИЕ: apply_verified_payment НЕ проверяет платёж. Её можно вызывать только из
доверенного серверного кода ПОСЛЕ проверки платежа у провайдера (подпись webhook,
сумма, валюта, идентификатор платежа). Публичного API для неё нет и быть не должно:
браузер не может сделать заказ оплаченным.

Что делает одна транзакция:
1. Блокирует заказ (SELECT ... FOR UPDATE).
   * уже "paid"                    -> {"status": "already_paid"}, ничего не меняется;
   * не "pending"/"requires_action" -> {"error": "invalid_order_status"}.
2. Блокирует все товары заказа в порядке возрастания id (одинаковый порядок во всех
   транзакциях — нет взаимных блокировок) и проверяет каждую позицию:
   товар в продаже; для inventory_mode = 'tracked' — остатка хватает.
3. Если хоть одной позиции не хватает — остатки НЕ меняются, заказ получает статус
   "requires_action" (деньги получены, выдать нельзя — нужно решение человека:
   дозаказ или возврат), возвращается {"error": "out_of_stock", "problems": [...]}.
   Повторный вызов после пополнения остатка доведёт заказ до "paid".
4. Иначе списывает остатки всех tracked-позиций (условный UPDATE, отрицательный
   остаток невозможен), ставит "paid" и paid_at, создаёт задачи исполнения
   (fulfillments, статус "pending") — идемпотентно, ON CONFLICT DO NOTHING.
Любая ошибка откатывает всё целиком.
"""

from database import get_connection


def apply_verified_payment(order_id):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT status
                FROM orders
                WHERE id = %s
                FOR UPDATE
                """,
                (order_id,)
            )

            order = cursor.fetchone()

            if order is None:
                return {"error": "order_not_found"}

            status = order[0]

            if status == "paid":
                return {"status": "already_paid"}

            if status not in ("pending", "requires_action"):
                return {"error": "invalid_order_status"}

            cursor.execute(
                """
                SELECT product_id, sum(quantity)
                FROM order_items
                WHERE order_id = %s
                GROUP BY product_id
                ORDER BY product_id
                """,
                (order_id,)
            )
            needed = [(product_id, int(quantity)) for product_id, quantity in cursor.fetchall()]

            if not needed:
                # После миграции 0002 у каждого заказа есть позиции; это защита от порчи данных.
                return {"error": "order_has_no_items"}

            cursor.execute(
                """
                SELECT id, quantity, is_for_sale, inventory_mode
                FROM products
                WHERE id = ANY(%s)
                ORDER BY id
                FOR UPDATE
                """,
                ([product_id for product_id, _ in needed],)
            )
            stock = {row[0]: row for row in cursor.fetchall()}

            problems = []
            for product_id, quantity in needed:
                _, available, is_for_sale, inventory_mode = stock[product_id]
                if not is_for_sale:
                    problems.append({"product_id": product_id, "reason": "not_for_sale"})
                elif inventory_mode == "tracked" and available < quantity:
                    problems.append({"product_id": product_id, "reason": "insufficient_stock"})

            if problems:
                cursor.execute(
                    "UPDATE orders SET status = 'requires_action' WHERE id = %s",
                    (order_id,)
                )
                return {
                    "error": "out_of_stock",
                    "order_id": order_id,
                    "status": "requires_action",
                    "problems": problems,
                }

            for product_id, quantity in needed:
                if stock[product_id][3] != "tracked":
                    continue
                cursor.execute(
                    """
                    UPDATE products
                    SET quantity = quantity - %s
                    WHERE id = %s
                      AND quantity >= %s
                    RETURNING id
                    """,
                    (quantity, product_id, quantity)
                )
                if cursor.fetchone() is None:
                    # Невозможно при удерживаемой блокировке; если случилось — откатить всё.
                    raise RuntimeError(f"остаток товара {product_id} изменился во время оплаты")

            cursor.execute(
                """
                UPDATE orders
                SET status = 'paid', paid_at = now()
                WHERE id = %s
                """,
                (order_id,)
            )

            cursor.execute(
                """
                INSERT INTO fulfillments (order_item_id, fulfillment_type)
                SELECT id, fulfillment_type
                FROM order_items
                WHERE order_id = %s
                ORDER BY id
                ON CONFLICT (order_item_id) DO NOTHING
                """,
                (order_id,)
            )

            return {
                "order_id": order_id,
                "status": "paid"
            }
