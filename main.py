product_0 = {
    "name": "Minecraft Key",
    "price": 999,
    "quantity": 15,
    "is_for_sale" : True
}
product_1 = {
    "name": "GTA V Key",
    "price": 1499 ,
    "quantity": 8,
    "is_for_sale" : True
}
product_2 = {
    "name": "Cyberpunk 2077 Key",
    "price": 1999,
    "quantity": 0,
    "is_for_sale" : False
}
products = [product_0, product_1, product_2]
for product in products:
    if product["is_for_sale"] and product["quantity"] > 0:
        print(f"{product['name']} - {product['price']} RUB - in stock: {product['quantity']}")
    else:
        print(f"{product['name']} is not for sale.")    