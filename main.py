products = [
    {"name": "Minecraft Key", "price": 999, "quantity": 15, "is_for_sale" : True},
    {"name": "GTA V Key", "price": 1499, "quantity": 0, "is_for_sale" : False},
	{"name": "Cyberpunk 2077", "price": 1999, "quantity": 9, "is_for_sale" : True}
]

def show_product(name, price, quantity):
    print(f"{name} - {price} RUB - in stock: {quantity}")
    
for product in products:
    if product["is_for_sale"] and product["quantity"] > 0:
        show_product(product["name"], product["price"], product["quantity"])
    else:
        print(f"{product['name']} Unavailable.")