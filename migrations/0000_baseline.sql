-- 0000 — базовая схема Digital Store (состояние до добавления статусов заказа).
--
-- На новой базе создаёт таблицы.
-- На существующей базе (digital_store, digital_store_test) таблицы уже есть,
-- поэтому CREATE TABLE IF NOT EXISTS ничего не делает, а проверка в конце
-- убеждается, что их структура совпадает с ожидаемой:
--   * колонки, типы и NOT NULL;
--   * значения по умолчанию (id — из последовательности, у остальных колонок — нет);
--   * PRIMARY KEY, FOREIGN KEY (включая ON DELETE/ON UPDATE, NOT VALID, DEFERRABLE)
--     и точный набор CHECK-ограничений — без недостающих и без лишних.
-- Если что-то не совпадает, миграция падает, и migrate.py откатывает всё —
-- включая создание журнала schema_migrations. База остаётся как была.
--
-- BEGIN/COMMIT здесь не пишутся: транзакцией управляет migrate.py.

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
    price INTEGER NOT NULL CHECK (price >= 0)
);

DO $$
DECLARE
    problems TEXT[];
BEGIN
    -- 1. Колонки, типы, NOT NULL.
    SELECT coalesce(array_agg(
               'колонка ' || e.tbl || '.' || e.col || ' должна быть ' || e.typ || ' NOT NULL'
               ORDER BY e.tbl, e.col),
           ARRAY[]::TEXT[])
    INTO problems
    FROM (VALUES
        ('products', 'id', 'integer'),
        ('products', 'name', 'text'),
        ('products', 'price', 'integer'),
        ('products', 'quantity', 'integer'),
        ('products', 'is_for_sale', 'boolean'),
        ('orders', 'id', 'integer'),
        ('orders', 'product_id', 'integer'),
        ('orders', 'price', 'integer')
    ) AS e(tbl, col, typ)
    LEFT JOIN information_schema.columns c
        ON c.table_schema = current_schema()
       AND c.table_name = e.tbl
       AND c.column_name = e.col
       AND c.data_type = e.typ
       AND c.is_nullable = 'NO'
    WHERE c.column_name IS NULL;

    -- 2. Значения по умолчанию.
    SELECT problems || coalesce(array_agg(
               CASE WHEN e.from_sequence
                    THEN 'у ' || e.tbl || '.' || e.col || ' нет DEFAULT из последовательности (SERIAL)'
                    ELSE 'у ' || e.tbl || '.' || e.col || ' неожиданный DEFAULT ' || c.column_default
               END
               ORDER BY e.tbl, e.col),
           ARRAY[]::TEXT[])
    INTO problems
    FROM (VALUES
        ('products', 'id', TRUE),
        ('products', 'name', FALSE),
        ('products', 'price', FALSE),
        ('products', 'quantity', FALSE),
        ('products', 'is_for_sale', FALSE),
        ('orders', 'id', TRUE),
        ('orders', 'product_id', FALSE),
        ('orders', 'price', FALSE)
    ) AS e(tbl, col, from_sequence)
    JOIN information_schema.columns c
        ON c.table_schema = current_schema()
       AND c.table_name = e.tbl
       AND c.column_name = e.col
    WHERE CASE WHEN e.from_sequence
               THEN coalesce(c.column_default, '') NOT LIKE 'nextval(%'
               ELSE c.column_default IS NOT NULL
          END;

    -- 3. PRIMARY KEY, FOREIGN KEY, CHECK: точное совпадение набора.
    --    Ограничение статуса из 0001 допускается (база, где 0001 уже применяли вручную),
    --    но только в точном виде.
    WITH expected(tbl, def) AS (
        VALUES
            ('products', 'PRIMARY KEY (id)'),
            ('products', 'CHECK ((price >= 0))'),
            ('products', 'CHECK ((quantity >= 0))'),
            ('orders', 'PRIMARY KEY (id)'),
            ('orders', 'CHECK ((price >= 0))'),
            ('orders', 'FOREIGN KEY (product_id) REFERENCES products(id)')
    ),
    allowed_extra(tbl, def) AS (
        VALUES
            ('orders', 'CHECK ((status = ANY (ARRAY[''pending''::text, ''paid''::text, ''cancelled''::text, ''legacy''::text])))')
    ),
    actual(tbl, def) AS (
        SELECT rel.relname::TEXT, pg_get_constraintdef(con.oid)
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        WHERE con.conrelid IN (to_regclass('products'), to_regclass('orders'))
          AND con.contype IN ('p', 'f', 'c')
    ),
    missing AS (
        SELECT tbl, def FROM expected
        EXCEPT
        SELECT tbl, def FROM actual
    ),
    unexpected AS (
        SELECT tbl, def FROM actual
        EXCEPT
        SELECT tbl, def FROM (SELECT * FROM expected UNION ALL SELECT * FROM allowed_extra) ok
    )
    SELECT problems
           || coalesce((SELECT array_agg('нет ограничения ' || tbl || ': ' || def ORDER BY tbl, def) FROM missing), ARRAY[]::TEXT[])
           || coalesce((SELECT array_agg('лишнее или изменённое ограничение ' || tbl || ': ' || def ORDER BY tbl, def) FROM unexpected), ARRAY[]::TEXT[])
    INTO problems;

    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION
            'Существующие таблицы не совпадают с базовой схемой Digital Store: %',
            array_to_string(problems, '; ');
    END IF;
END
$$;
