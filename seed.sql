INSERT INTO products (id, name, price, quantity, is_for_sale)
VALUES
    (1, 'Minecraft Key', 999, 15, TRUE),
    (2, 'GTA V Key', 1499, 0, FALSE),
    (3, 'Cyberpunk 2077', 1999, 9, TRUE)
ON CONFLICT (id) DO NOTHING;

SELECT setval(
    pg_get_serial_sequence('products', 'id'),
    GREATEST((SELECT MAX(id) FROM products), 1)
);