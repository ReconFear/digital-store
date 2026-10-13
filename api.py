from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from database import (
    create_order_in_db,
    get_public_product_from_db,
    get_public_products_from_db,
)
from cors_settings import cors_middleware_options
from commerce import catalog as catalog_v2
from commerce.orders import OrderError
from commerce.schemas import (
    CheckoutV2,
    CreateOrderIn,
    ErrorV2,
    OrderAccessIn,
    OrderCreatedV2,
    OrderStatusV2,
    ProductListV2,
    ProductV2,
)
from http_security import RequestLimitsMiddleware, client_ip, request_limits_options
from payments import PaymentServiceError, build_payment_service
from payments.access import create_order_with_access

app = FastAPI()

# Ограничение частоты и размера запросов (http_security.py). Добавлено ДО CORS, поэтому
# CORS остаётся внешним слоем и ответы 413/429 тоже получают CORS-заголовки.
_limits = request_limits_options()
app.add_middleware(RequestLimitsMiddleware, **_limits)

# Какие frontend-адреса браузер пустит к API. Настраивается переменной
# CORS_ALLOWED_ORIGINS (см. cors_settings.py). CORS — не защита API.
app.add_middleware(CORSMiddleware, **cors_middleware_options())

# Платежи (PR 3). По умолчанию выключены: PAYMENTS_ENABLED=false (см. PAYMENTS.md).
# Неверная конфигурация платежей останавливает запуск — без молчаливого отключения.
payments_v2 = build_payment_service()


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
    # Заказ создаётся в статусе "pending". Оплата — отдельным запросом checkout.
    # access_token — секрет владельца заказа (PR 3): показывается один раз, в базе только хэш.
    try:
        order, access_token = create_order_with_access([(item.product_id, item.quantity) for item in request.items])
    except OrderError as error:
        raise HTTPException(status_code=error.http_status, detail=error.as_detail())

    return {"order": order, "access_token": access_token}


# ---------------------------------------------------------------------------
# Оплата (PR 3). Доступ к заказу — только по паре (id, access_token) в ТЕЛЕ запроса
# (не в URL: адреса попадают в логи и историю браузера). Неверный секрет и
# несуществующий заказ неотличимы (404). Оплачен ли заказ, решает только платёжный
# обработчик по ответу провайдера — ни один из этих маршрутов не делает заказ оплаченным.
# ---------------------------------------------------------------------------

_PAYMENT_ERRORS = {404: {"model": ErrorV2}, 409: {"model": ErrorV2}, 503: {"model": ErrorV2}}


def _payment_error(error):
    raise HTTPException(status_code=error.http_status, detail=error.as_detail())


@app.post("/api/v2/orders/{order_id}/checkout", response_model=CheckoutV2, responses=_PAYMENT_ERRORS)
def checkout_v2(order_id: int, request: OrderAccessIn):
    try:
        return payments_v2.checkout(order_id, request.access_token)
    except PaymentServiceError as error:
        _payment_error(error)


@app.post("/api/v2/orders/{order_id}/status", response_model=OrderStatusV2, responses={404: {"model": ErrorV2}})
def order_status_v2(order_id: int, request: OrderAccessIn):
    try:
        return payments_v2.order_status(order_id, request.access_token)
    except PaymentServiceError as error:
        _payment_error(error)


@app.post("/api/v2/payments/yookassa/webhook", include_in_schema=False)
async def yookassa_webhook(request: Request):
    # Уведомление только ставится в очередь; ответ 200 — после COMMIT в provider_events.
    # Ошибка базы -> 500, и ЮKassa повторит доставку. Фиктивного успеха нет.
    body = await request.body()
    ip = client_ip(request.scope, _limits["trusted_proxies"])
    try:
        await run_in_threadpool(payments_v2.accept_notification, body, ip)
    except PaymentServiceError as error:
        return JSONResponse(status_code=error.http_status, content={"detail": error.as_detail()})
    return {}
