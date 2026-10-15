"""Каталог v1 из PostgreSQL. Данные — свои, в откатываемой транзакции (не seed.sql)."""

from database import get_products_from_db


def test_get_products_from_postgresql(shared_test_connection, v1_product):
    created = [v1_product(price=999), v1_product(price=1999, quantity=0), v1_product(price=2999)]

    products = [p for p in get_products_from_db() if p["id"] in created]

    assert [p["id"] for p in products] == created          # порядок — по id
    assert [p["price"] for p in products] == [999, 1999, 2999]
    assert [p["quantity"] for p in products] == [10, 0, 10]
    assert all(p["name"].startswith("pytest-v1-") for p in products)
