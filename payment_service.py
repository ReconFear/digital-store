from database import get_connection


def apply_verified_payment(order_id):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT product_id, status
                FROM orders
                WHERE id = %s
                FOR UPDATE
                """,
                (order_id,)
            )

            order = cursor.fetchone()

            if order is None:
                return {"error": "order_not_found"}

            product_id, status = order

            if status == "paid":
                return {"status": "already_paid"}

            if status != "pending":
                return {"error": "invalid_order_status"}

            cursor.execute(
                """
                UPDATE products
                SET quantity = quantity - 1
                WHERE id = %s
                  AND quantity > 0
                  AND is_for_sale = TRUE
                RETURNING id
                """,
                (product_id,)
            )

            if cursor.fetchone() is None:
                return {"error": "out_of_stock"}

            cursor.execute(
                """
                UPDATE orders
                SET status = 'paid'
                WHERE id = %s
                """,
                (order_id,)
            )

            return {
                "order_id": order_id,
                "status": "paid"
            }