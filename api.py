from fastapi import FastAPI, HTTPException
from products import products

app = FastAPI()
@app.get("/products")
def get_products(available_only: bool = False):
    available_products = []

    for product in products:
        if product["is_for_sale"] and product["quantity"] > 0:
            available_products.append(product)
    if available_only:
        return {"products": available_products}
    return {"products": products, "total": len(products)}
@app.get("/products/{product_id}")
def get_product(product_id: int):
    for product in products:
        if product["id"] == product_id:
            return product

    raise HTTPException(status_code=404, detail="Product not found")