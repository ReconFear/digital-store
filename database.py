import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


def get_connection():
    return psycopg.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )


def get_products_from_db():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, name, price, quantity, is_for_sale FROM products ORDER BY id"
            )

            rows = cursor.fetchall()
            products = []

            for row in rows:
                product = {
                    "id": row[0],
                    "name": row[1],
                    "price": row[2],
                    "quantity": row[3],
                    "is_for_sale": row[4]
                }

                products.append(product)

            return products


def create_order_in_db(product_id):
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                UPDATE products
                SET quantity = quantity - 1
                WHERE id = %s
                  AND quantity > 0
                  AND is_for_sale = TRUE
                RETURNING id, price
                """,
                (product_id,)
            )

            product = cursor.fetchone()

            if product is None:
                cursor.execute(
                    "SELECT id FROM products WHERE id = %s",
                    (product_id,)
                )

                if cursor.fetchone() is None:
                    return {"error": "not_found"}

                return {"error": "unavailable"}

            cursor.execute(
                """
                INSERT INTO orders (product_id, price)
                VALUES (%s, %s)
                RETURNING id
                """,
                (product[0], product[1])
            )

            order_id = cursor.fetchone()[0]

            return {
                "id": order_id,
                "product_id": product[0],
                "price": product[1]
            }


def get_orders_from_db():
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, product_id, price FROM orders ORDER BY id"
            )

            rows = cursor.fetchall()
            orders = []

            for row in rows:
                order = {
                    "id": row[0],
                    "product_id": row[1],
                    "price": row[2]
                }

                orders.append(order)

            return orders