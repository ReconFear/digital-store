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
                SELECT id, price, quantity, is_for_sale
                FROM products
                WHERE id = %s
                """,
                (product_id,)
            )

            product = cursor.fetchone()

            if product is None:
                return {"error": "not_found"}

            if product[2] <= 0 or not product[3]:
                return {"error": "unavailable"}

            cursor.execute(
                """
                INSERT INTO orders (product_id, price, status)
                VALUES (%s, %s, 'pending')
                RETURNING id
                """,
                (product[0], product[1])
            )

            order_id = cursor.fetchone()[0]

            return {
                "id": order_id,
                "product_id": product[0],
                "price": product[1],
                "status": "pending"
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