from fastapi import FastAPI
from products import products

app = FastAPI()
@app.get("/products")
def get_products():
    return {"products": products}