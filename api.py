from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from database import (
    create_order_in_db,
    get_public_product_from_db,
    get_public_products_from_db,
)
from cors_settings import cors_middleware_options
from commerce import catalog as catalog_v2
from commerce.orders import OrderError, create_order as create_order_v2
from commerce.schemas import CreateOrderIn, ErrorV2, OrderCreatedV2, ProductListV2, ProductV2

app = FastAPI()

# Какие frontend-адреса браузер пустит к API. Настраивается переменной
# CORS_ALLOWED_ORIGINS (см. cors_settings.py). CORS — не защита API.
app.add_middleware(CORSMiddleware, **cors_middleware_options())


class OrderRequest(BaseModel):
    product_id: int = Field(gt=0)


@app.get("/products")
def get_products(available_only: bool = False):
    # Публичный каталог: товары, снятые с продажи (is_for_sale = false), не показываются.
    products = get_public_products_from_db(available_only=available_only)

    return {
        "products": products,
        "total": len(products)
    }


@app.get("/products/{product_id}")
def get_product(product_id: int):
    product = get_public_product_from_db(product_id)

    # Снятый с продажи товар для покупателя неотличим от несуществующего.
    if product is None:
        raise HTTPException(
            status_code=404,
            detail="Product not found"
        )

    return product


@app.post("/orders")
def create_order(order: OrderRequest):
    result = create_order_in_db(order.product_id)

    if result.get("error") == "not_found":
        raise HTTPException(
            status_code=404,
            detail="Product not found"
        )

    if result.get("error") == "unavailable":
        raise HTTPException(
            status_code=409,
            detail="Product unavailable"
        )

    return {
        "message": "Order created",
        "order": result
    }


@app.get("/orders")
def get_orders():
    raise HTTPException(
        status_code=403,
        detail="Order listing is not public"
    )


# ---------------------------------------------------------------------------
# API v2 — универсальное ядро (PR 2). Старые маршруты выше не менялись.
# Деньги — целые минимальные единицы + валюта. Ошибки бизнес-правил:
#   {"detail": {"code": "...", "message": "...", "product_ids": [...]}}
# Ошибки формата запроса — стандартный 422 FastAPI ({"detail": [ ... ]}).
# Чтения заказа по id нет намеренно: без аутентификации это раскрыло бы чужие
# заказы перебором id (см. COMMERCE_CORE.md).
# ---------------------------------------------------------------------------

_V2_ERRORS = {404: {"model": ErrorV2}}


def _v2_not_found(product_id):
    raise HTTPException(
        status_code=404,
        detail={"code": "product_not_found", "message": "Товар не найден или не продаётся.",
                "product_ids": [product_id]},
    )


@app.get("/api/v2/products", response_model=ProductListV2)
def get_products_v2(available_only: bool = False):
    products = catalog_v2.get_public_products(available_only=available_only)

    return {"products": products, "total": len(products)}


@app.get("/api/v2/products/{product_id}", response_model=ProductV2, responses=_V2_ERRORS)
def get_product_v2(product_id: int):
    product = catalog_v2.get_public_product(product_id) if product_id > 0 else None

    if product is None:
        _v2_not_found(product_id)

    return product


@app.post(
    "/api/v2/orders",
    status_code=201,
    response_model=OrderCreatedV2,
    responses={404: {"model": ErrorV2}, 409: {"model": ErrorV2}, 422: {"model": ErrorV2}},
)
def create_order_v2_endpoint(request: CreateOrderIn):
    # Заказ создаётся в статусе "pending". Оплата и выдача — не здесь.
    try:
        order = create_order_v2([(item.product_id, item.quantity) for item in request.items])
    except OrderError as error:
        raise HTTPException(status_code=error.http_status, detail=error.as_detail())

    return {"order": order}
