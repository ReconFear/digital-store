from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from products import products

app = FastAPI()

orders = []
class OrderRequest(BaseModel):
    product_id: int
@app.get("/products")
def get_products(available_only: bool = False):
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
    for product in products:
        if product["id"] == product_id:
            return product

    raise HTTPException(
        status_code=404,
        detail="Product not found"
    )
@app.post("/orders")
def create_order(order: OrderRequest):
    for product in products:
        if product["id"] == order.product_id:

            if not product["is_for_sale"] or product["quantity"] <= 0:
                raise HTTPException(
                    status_code=409,
                    detail="Product unavailable"
                )

            new_order = {
                "id": len(orders) + 1,
                "product_id": product["id"],
                "price": product["price"]
            }

            orders.append(new_order)
            product["quantity"] -= 1

            return {
                "message": "Order created",
                "order": new_order
            }

    raise HTTPException(status_code=404, detail="Product not found")