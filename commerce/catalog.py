"""Каталог в новом формате (API v2).

Публичны только товары в продаже (is_for_sale) в поддерживаемой валюте.
Внутренние детали исполнения (fulfillment_type) наружу не отдаются.
"""

import database
from commerce.money import CURRENCY_EXPONENTS, money_json

# quantity — продаваемый остаток (PR5): у товаров с автоматической выдачей ключей не больше
# числа свободных ключей за вычетом ожидающих выдачи (database.SELLABLE_QUANTITY).
_COLUMNS = f"id, name, product_type, inventory_mode, price_minor, currency, {database.SELLABLE_QUANTITY}"


def _product_v2(row):
    product_id, name, product_type, inventory_mode, price_minor, currency, quantity = row
    tracked = inventory_mode == "tracked"
    return {
        "id": product_id,
        "name": name,
        "product_type": product_type,
        "inventory_mode": inventory_mode,
        "price": money_json(price_minor, currency),
        "stock_quantity": quantity if tracked else None,
        "available": (quantity > 0) if tracked else True,
    }


def _public_filter():
    return "is_for_sale = TRUE AND currency = ANY(%s)"


def get_public_products(available_only=False):
    query = f"SELECT {_COLUMNS} FROM products WHERE {_public_filter()}"
    if available_only:
        query += f" AND (inventory_mode = 'unlimited' OR {database.SELLABLE_QUANTITY} > 0)"
    query += " ORDER BY id"

    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(query, (list(CURRENCY_EXPONENTS),))
            return [_product_v2(row) for row in cursor.fetchall()]


def get_public_product(product_id):
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                f"SELECT {_COLUMNS} FROM products WHERE id = %s AND {_public_filter()}",
                (product_id, list(CURRENCY_EXPONENTS)),
            )
            row = cursor.fetchone()
            return None if row is None else _product_v2(row)
