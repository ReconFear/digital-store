"""Публичный каталог на реальной тестовой базе.

Тест вставляет собственные товары и всё откатывает (фикстура shared_test_connection),
поэтому не зависит от содержимого seed.sql и не оставляет следов.
"""

import database


def _insert_product(conn, name, quantity, is_for_sale):
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO products (name, price, quantity, is_for_sale)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (name, 100, quantity, is_for_sale)
        )
        return cursor.fetchone()[0]


def test_public_catalog_hides_products_not_for_sale(shared_test_connection):
    conn = shared_test_connection
    visible_id = _insert_product(conn, "PR1 visible", 5, True)
    sold_out_id = _insert_product(conn, "PR1 sold out", 0, True)
    hidden_id = _insert_product(conn, "PR1 hidden", 5, False)

    public_ids = {p["id"] for p in database.get_public_products_from_db()}
    available_ids = {
        p["id"] for p in database.get_public_products_from_db(available_only=True)
    }

    assert visible_id in public_ids
    assert sold_out_id in public_ids  # в продаже, но закончился — показываем
    assert hidden_id not in public_ids

    assert visible_id in available_ids
    assert sold_out_id not in available_ids
    assert hidden_id not in available_ids

    assert all(p["is_for_sale"] for p in database.get_public_products_from_db())


def test_public_single_product_hides_products_not_for_sale(shared_test_connection):
    conn = shared_test_connection
    visible_id = _insert_product(conn, "PR1 visible", 5, True)
    hidden_id = _insert_product(conn, "PR1 hidden", 5, False)

    assert database.get_public_product_from_db(visible_id)["name"] == "PR1 visible"
    assert database.get_public_product_from_db(hidden_id) is None
    assert database.get_public_product_from_db(2_000_000_000) is None


def test_full_product_list_keeps_hidden_products_for_admin(shared_test_connection):
    conn = shared_test_connection
    hidden_id = _insert_product(conn, "PR1 hidden", 5, False)

    all_ids = {p["id"] for p in database.get_products_from_db()}

    assert hidden_id in all_ids
