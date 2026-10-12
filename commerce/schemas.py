"""Схемы запросов и ответов API v2 (pydantic).

Запросы строгие: целые числа без преобразования строк и bool, лишние поля запрещены.
Поэтому клиент физически не может передать цену, итог или статус заказа — такой
запрос получит 422.
"""

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from commerce.money import MAX_ITEM_QUANTITY, MAX_ORDER_ITEMS

ProductId = Annotated[int, Field(strict=True, gt=0, le=2_147_483_647)]
Quantity = Annotated[int, Field(strict=True, gt=0, le=MAX_ITEM_QUANTITY)]


class OrderItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: ProductId
    quantity: Quantity


class CreateOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[OrderItemIn] = Field(min_length=1, max_length=MAX_ORDER_ITEMS)


class Money(BaseModel):
    amount_minor: int = Field(description="Сумма в минимальных единицах валюты (копейках для RUB)")
    currency: str = Field(description="Код валюты ISO 4217")
    exponent: int = Field(description="Знаков после запятой: amount = amount_minor / 10^exponent")


class ProductV2(BaseModel):
    id: int
    name: str
    product_type: Literal["digital_key", "digital_file", "physical", "service", "other"]
    inventory_mode: Literal["tracked", "unlimited"]
    price: Money
    stock_quantity: Optional[int] = Field(description="Остаток; null, если остаток не учитывается")
    available: bool


class ProductListV2(BaseModel):
    products: list[ProductV2]
    total: int


class OrderItemOut(BaseModel):
    product_id: int
    name: str = Field(description="Название на момент заказа")
    product_type: str
    quantity: int
    unit_price: Money
    line_total: Money


class OrderOut(BaseModel):
    id: int
    status: Literal["pending"]
    created_at: str
    total: Money
    items: list[OrderItemOut]


class OrderCreatedV2(BaseModel):
    order: OrderOut


class ErrorDetailV2(BaseModel):
    code: str
    message: str
    product_ids: list[int] = []


class ErrorV2(BaseModel):
    detail: ErrorDetailV2
