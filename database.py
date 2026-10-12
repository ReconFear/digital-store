import os

import psycopg
from dotenv import load_dotenv

load_dotenv()


PRODUCT_COLUMNS = "id, name, price, quantity, is_for_sale"


def get_connection():
    return psycopg.connect(
        host=os.getenv("DB_HOST"),
        port=os.getenv("DB_PORT"),
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD")
    )


def _product_from_row(row):
    return {
        "id": row[0],
        "name": row[1],
        "price": row[2],
        "quantity": row[3],
        "is_for_sale": row[4]
    }


def get_products_from_db():
    """Все товары, включая снятые с продажи.

    Только для внутреннего и будущего административного использования.
    Не отдавать публично: покупатели должны получать get_public_products_from_db().
    """
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT {PRODUCT_COLUMNS} FROM products ORDER BY id"
            )

            return [_product_from_row(row) for row in cursor.fetchall()]


def get_public_products_from_db(available_only=False):
    """Каталог для покупателей: только товары в продаже (is_for_sale = TRUE).

    available_only=True дополнительно убирает товары с нулевым остатком.
    """
    query = f"SELECT {PRODUCT_COLUMNS} FROM products WHERE is_for_sale = TRUE"

    if available_only:
        query += " AND quantity > 0"

    query += " ORDER BY id"

    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query)

            return [_product_from_row(row) for row in cursor.fetchall()]


def get_public_product_from_db(product_id):
    """Один товар для покупателя или None, если товара нет или он снят с продажи."""
    with get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"""
                SELECT {PRODUCT_COLUMNS}
                FROM products
                WHERE id = %s
                  AND is_for_sale = TRUE
                """,
                (product_id,)
            )

            row = cursor.fetchone()

            return None if row is None else _product_from_row(row)


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
