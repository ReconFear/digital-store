from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from database import (
    create_order_in_db,
    get_public_product_from_db,
    get_public_products_from_db,
)
from cors_settings import cors_middleware_options

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
