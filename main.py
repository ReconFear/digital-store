from products import products

def show_product(name, price, quantity):
    print(f"{name} - {price} RUB - in stock: {quantity}")
    
for product in products:
    if product["is_for_sale"] and product["quantity"] > 0:
        show_product(product["name"], product["price"], product["quantity"])
    else:
        print(f"{product['name']} Unavailable.")