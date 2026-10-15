"""Создание многопозиционного заказа (API v2).

Правила:
* Клиент присылает только product_id и quantity. Цены, валюту и итоги считает сервер.
* Повторяющиеся product_id объединяются: количества складываются (порядок позиций —
  по первому появлению). Итоговое количество проверяется после объединения.
* Товар должен существовать и быть в продаже; скрытый товар неотличим от несуществующего.
* Для товаров с учётом остатка (tracked) остатка должно хватать на момент создания.
  Это проверка, а не резерв: остаток списывается только при подтверждённой оплате.
* Все позиции заказа — в одной валюте; смешивать валюты нельзя.
* Цена, название и тип каждой позиции фиксируются в заказе (снимок).
* Всё выполняется в одной транзакции: при любой ошибке заказ не создаётся вовсе.
* Заказ создаётся в статусе "pending". Оплаченным его может сделать только
  payment_service.apply_verified_payment — не этот модуль и не запрос браузера.
"""

from dataclasses import dataclass, field

import database
from commerce.money import (
    MAX_ITEM_QUANTITY,
    MAX_ORDER_ITEMS,
    MoneyError,
    is_supported_currency,
    line_total_minor,
    money_json,
    order_total_minor,
)


@dataclass
class OrderError(Exception):
    """Ошибка создания заказа, понятная клиенту API.

    code — машинный код; http_status — рекомендуемый HTTP-статус;
    product_ids — к каким товарам относится ошибка (если относится).
    """

    code: str
    http_status: int
    message: str
    product_ids: list = field(default_factory=list)

    def __str__(self):
        return f"{self.code}: {self.message}"

    def as_detail(self):
        return {"code": self.code, "message": self.message, "product_ids": self.product_ids}


def merge_items(items):
    """[(product_id, quantity), ...] -> {product_id: quantity} без потери порядка."""
    merged = {}
    for product_id, quantity in items:
        merged[product_id] = merged.get(product_id, 0) + quantity
    return merged


def create_order(items, on_created=None):
    """Создаёт заказ "pending" из позиций [(product_id, quantity), ...].

    on_created(cursor, order_id) — необязательный шаг в ТОЙ ЖЕ транзакции после записи
    позиций (PR 3: секрет доступа к заказу, payments/access.py). Ошибка в нём отменяет заказ.

    Типы и диапазоны входных данных проверяет схема API (commerce/schemas.py);
    здесь — бизнес-правила, которые зависят от состояния базы.
    """
    if not items:
        raise OrderError("empty_order", 422, "В заказе нет позиций.")

    merged = merge_items(items)
    if len(merged) > MAX_ORDER_ITEMS:
        raise OrderError("too_many_items", 422, f"Не больше {MAX_ORDER_ITEMS} разных товаров в заказе.")
    too_many = [pid for pid, qty in merged.items() if qty > MAX_ITEM_QUANTITY]
    if too_many:
        raise OrderError(
            "quantity_too_large", 422,
            f"Не больше {MAX_ITEM_QUANTITY} единиц одного товара в заказе.", too_many,
        )

    product_ids = list(merged)

    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            # Продаваемый остаток (PR5) — подсказка покупателю. Гарантию даёт apply_paid_order при оплате.
            cursor.execute(
                f"""
                SELECT id, name, price_minor, currency, {database.SELLABLE_QUANTITY}, is_for_sale,
                       inventory_mode, product_type, fulfillment_type
                FROM products
                WHERE id = ANY(%s)
                """,
                (product_ids,),
            )
            products = {row[0]: row for row in cursor.fetchall()}

            missing = [pid for pid in product_ids if pid not in products or not products[pid][5]]
            if missing:
                raise OrderError("product_not_found", 404, "Товар не найден или не продаётся.", missing)

            currencies = {products[pid][3] for pid in product_ids}
            if len(currencies) > 1:
                raise OrderError(
                    "mixed_currency", 409,
                    "Товары в разных валютах нельзя оформить одним заказом.", product_ids,
                )
            currency = currencies.pop()
            if not is_supported_currency(currency):
                raise OrderError("unsupported_currency", 409, f"Валюта {currency} не поддерживается.", product_ids)

            short = [
                pid for pid in product_ids
                if products[pid][6] == "tracked" and products[pid][4] < merged[pid]
            ]
            if short:
                raise OrderError("insufficient_stock", 409, "Недостаточно товара в наличии.", short)

            lines = []
            try:
                for pid in product_ids:
                    _, name, price_minor, _, _, _, _, product_type, fulfillment_type = products[pid]
                    quantity = merged[pid]
                    lines.append({
                        "product_id": pid,
                        "name": name,
                        "product_type": product_type,
                        "fulfillment_type": fulfillment_type,
                        "quantity": quantity,
                        "unit_price_minor": price_minor,
                        "line_total_minor": line_total_minor(price_minor, quantity),
                    })
                total = order_total_minor(line["line_total_minor"] for line in lines)
            except MoneyError as exc:
                raise OrderError("amount_out_of_range", 422, "Сумма заказа вне допустимого диапазона.") from exc

            cursor.execute(
                """
                INSERT INTO orders (status, currency, total_minor)
                VALUES ('pending', %s, %s)
                RETURNING id, created_at
                """,
                (currency, total),
            )
            order_id, created_at = cursor.fetchone()

            for line in lines:
                cursor.execute(
                    """
                    INSERT INTO order_items (
                        order_id, product_id, product_name, product_type, fulfillment_type,
                        quantity, unit_price_minor, currency, line_total_minor
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        order_id, line["product_id"], line["name"], line["product_type"],
                        line["fulfillment_type"], line["quantity"], line["unit_price_minor"],
                        currency, line["line_total_minor"],
                    ),
                )

            if on_created is not None:
                on_created(cursor, order_id)

    return {
        "id": order_id,
        "status": "pending",
        "created_at": created_at if isinstance(created_at, str) else created_at.isoformat(),
        "total": money_json(total, currency),
        "items": [
            {
                "product_id": line["product_id"],
                "name": line["name"],
                "product_type": line["product_type"],
                "quantity": line["quantity"],
                "unit_price": money_json(line["unit_price_minor"], currency),
                "line_total": money_json(line["line_total_minor"], currency),
            }
            for line in lines
        ],
    }
