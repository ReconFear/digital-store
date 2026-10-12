from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from database import (
    create_order_in_db,
    get_public_product_from_db,
    get_public_products_from_db,
)

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:5173",
        "http://127.0.0.1:5173"
    ],
    allow_methods=["*"],
    allow_headers=["*"]
)


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
