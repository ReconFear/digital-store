from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from products import products
from database import get_products_from_db, create_order_in_db, get_orders_from_db

app = FastAPI()

orders = []
class OrderRequest(BaseModel):
    product_id: int
@app.get("/products")
def get_products(available_only: bool = False):
    products = get_products_from_db()
    available_products = []

    for product in products:
        if product["is_for_sale"] and product["quantity"] > 0:
            available_products.append(product)

    if available_only:
        return {
            "products": available_products,
            "total": len(available_products)
        }

    return {
        "products": products,
        "total": len(products)
    }

    raise HTTPException(status_code=404, detail="Product not found")
@app.get("/products/{product_id}")
def get_product(product_id: int):
    products = get_products_from_db()
    for product in products:
        if product["id"] == product_id:
            return product

    raise HTTPException(
        status_code=404,
        detail="Product not found"
    )
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
    orders = get_orders_from_db()

    return {
        "orders": orders,
        "total": len(orders)
    }