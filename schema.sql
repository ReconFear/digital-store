SET client_encoding = 'UTF8';

-- Полная текущая схема Digital Store — справочный снимок.
--
-- Устанавливать базу нужно через систему миграций:
--     python migrate.py apply --database <имя_базы>
-- Миграции в папке migrations/ — единственный источник истины.
-- Этот файл должен давать ту же структуру, что и все миграции вместе;
-- это проверяет тест tests/test_migrations.py::test_schema_sql_matches_migrations.
-- Меняя схему, добавляй новую миграцию и обновляй этот файл.

CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    price INTEGER NOT NULL CHECK (price >= 0),
    quantity INTEGER NOT NULL CHECK (quantity >= 0),
    is_for_sale BOOLEAN NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    price INTEGER NOT NULL CHECK (price >= 0),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'paid', 'cancelled', 'legacy'))
);
