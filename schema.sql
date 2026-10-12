SET client_encoding = 'UTF8';

-- Полная текущая схема Digital Store — справочный снимок (после миграции 0002).
--
-- Устанавливать базу нужно через систему миграций:
--     python migrate.py apply --database <имя_базы>
-- Миграции в папке migrations/ — единственный источник истины.
-- Этот файл должен давать ту же структуру (таблицы, ограничения, индексы, функции,
-- триггеры), что и все миграции вместе; это проверяет тест
-- tests/test_migrations.py::test_schema_sql_matches_migrations.
-- Меняя схему, добавляй новую миграцию и обновляй этот файл.
-- Описание модели — в COMMERCE_CORE.md.

CREATE TABLE products (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    price INTEGER CHECK (price >= 0),                 -- устаревшее: целые рубли, зеркало price_minor
    quantity INTEGER NOT NULL CHECK (quantity >= 0),   -- остаток; учитывается при inventory_mode = 'tracked'
    is_for_sale BOOLEAN NOT NULL,
    price_minor BIGINT NOT NULL,                       -- цена в минимальных единицах (копейках)
    currency TEXT NOT NULL DEFAULT 'RUB',
    product_type TEXT NOT NULL DEFAULT 'other',
    inventory_mode TEXT NOT NULL DEFAULT 'tracked',
    fulfillment_type TEXT NOT NULL DEFAULT 'manual',
    CONSTRAINT products_price_minor_check
        CHECK (price_minor >= 0 AND price_minor <= 1000000000000),
    CONSTRAINT products_currency_check
        CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT products_legacy_price_check
        CHECK (price IS NULL OR (currency = 'RUB' AND price_minor = price::BIGINT * 100)),
    CONSTRAINT products_product_type_check
        CHECK (product_type IN ('digital_key', 'digital_file', 'physical', 'service', 'other')),
    CONSTRAINT products_inventory_mode_check
        CHECK (inventory_mode IN ('tracked', 'unlimited')),
    CONSTRAINT products_fulfillment_type_check
        CHECK (fulfillment_type IN ('manual', 'digital_keys', 'downloads', 'physical_shipping')),
    CONSTRAINT products_type_combination_check CHECK (
           (product_type = 'digital_key'  AND inventory_mode = 'tracked' AND fulfillment_type IN ('digital_keys', 'manual'))
        OR (product_type = 'digital_file' AND fulfillment_type IN ('downloads', 'manual'))
        OR (product_type = 'physical'     AND inventory_mode = 'tracked' AND fulfillment_type IN ('physical_shipping', 'manual'))
        OR (product_type = 'service'      AND fulfillment_type = 'manual')
        OR (product_type = 'other'        AND fulfillment_type = 'manual')
    ),
    CONSTRAINT products_unlimited_quantity_check
        CHECK (inventory_mode = 'tracked' OR quantity = 0)
);

CREATE TABLE orders (
    id SERIAL PRIMARY KEY,
    product_id INTEGER REFERENCES products(id),        -- устаревшее: только у заказов v1
    price INTEGER CHECK (price >= 0),                 -- устаревшее: только у заказов v1, целые рубли
    status TEXT NOT NULL DEFAULT 'pending',
    currency TEXT NOT NULL,
    total_minor BIGINT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now(),             -- NULL у заказов, созданных до 0002
    paid_at TIMESTAMPTZ,
    items_sealed BOOLEAN NOT NULL DEFAULT FALSE,       -- состав закрыт (ставится при COMMIT создания)
    CONSTRAINT orders_status_check
        CHECK (status IN ('pending', 'paid', 'cancelled', 'legacy', 'requires_action')),
    CONSTRAINT orders_currency_check
        CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT orders_total_minor_check
        CHECK (total_minor >= 0),
    CONSTRAINT orders_legacy_shape_check
        CHECK ((product_id IS NULL) = (price IS NULL)),
    CONSTRAINT orders_legacy_total_check
        CHECK (product_id IS NULL OR (currency = 'RUB' AND total_minor = price::BIGINT * 100)),
    CONSTRAINT orders_paid_at_check
        CHECK (paid_at IS NULL OR status = 'paid')
);

CREATE TABLE order_items (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    product_name TEXT NOT NULL,
    product_type TEXT NOT NULL,
    fulfillment_type TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    unit_price_minor BIGINT NOT NULL,
    currency TEXT NOT NULL,
    line_total_minor BIGINT NOT NULL,
    CONSTRAINT order_items_quantity_check CHECK (quantity > 0 AND quantity <= 1000),
    CONSTRAINT order_items_unit_price_minor_check
        CHECK (unit_price_minor >= 0 AND unit_price_minor <= 1000000000000),
    CONSTRAINT order_items_currency_check CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT order_items_line_total_check CHECK (line_total_minor = unit_price_minor * quantity),
    CONSTRAINT order_items_order_product_key UNIQUE (order_id, product_id)
);

CREATE INDEX order_items_product_id_idx ON order_items (product_id);

CREATE TABLE fulfillments (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_item_id BIGINT NOT NULL REFERENCES order_items(id),
    fulfillment_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_attempt_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    fulfilled_at TIMESTAMPTZ,
    CONSTRAINT fulfillments_order_item_key UNIQUE (order_item_id),
    CONSTRAINT fulfillments_fulfillment_type_check
        CHECK (fulfillment_type IN ('manual', 'digital_keys', 'downloads', 'physical_shipping')),
    CONSTRAINT fulfillments_status_check
        CHECK (status IN ('pending', 'in_progress', 'fulfilled', 'requires_action', 'failed', 'cancelled')),
    CONSTRAINT fulfillments_attempts_check CHECK (attempts >= 0),
    CONSTRAINT fulfillments_last_error_check CHECK (char_length(last_error) <= 300),
    CONSTRAINT fulfillments_fulfilled_at_check CHECK ((status = 'fulfilled') = (fulfilled_at IS NOT NULL))
);

CREATE INDEX fulfillments_pending_idx ON fulfillments (id) WHERE status = 'pending';

-- Функции и триггеры (копия из migrations/0002_universal_core.sql).

CREATE FUNCTION products_sync_legacy_price() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    legacy_given BOOLEAN;
    minor_given BOOLEAN;
BEGIN
    IF TG_OP = 'INSERT' THEN
        legacy_given := NEW.price IS NOT NULL;
        minor_given := NEW.price_minor IS NOT NULL;
    ELSE
        legacy_given := NEW.price IS DISTINCT FROM OLD.price;
        minor_given := NEW.price_minor IS DISTINCT FROM OLD.price_minor
                       OR NEW.currency IS DISTINCT FROM OLD.currency;
    END IF;

    IF legacy_given AND NOT minor_given THEN
        IF NEW.price IS NULL THEN
            RAISE EXCEPTION 'products.price нельзя обнулить: меняйте price_minor и currency';
        END IF;
        IF NEW.currency <> 'RUB' THEN
            RAISE EXCEPTION 'устаревшее поле products.price (целые рубли) допустимо только для RUB, а валюта товара %', NEW.currency;
        END IF;
        NEW.price_minor := NEW.price::BIGINT * 100;
        RETURN NEW;
    END IF;

    IF NEW.price_minor IS NULL THEN
        RAISE EXCEPTION 'у товара должна быть цена: price_minor (или устаревший price в рублях)';
    END IF;

    IF legacy_given THEN
        RETURN NEW;  -- заданы оба поля: согласованность проверит products_legacy_price_check
    END IF;

    IF NEW.currency = 'RUB' AND NEW.price_minor % 100 = 0 AND NEW.price_minor / 100 <= 2147483647 THEN
        NEW.price := (NEW.price_minor / 100)::INTEGER;
    ELSE
        NEW.price := NULL;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER products_sync_legacy_price
    BEFORE INSERT OR UPDATE OF price, price_minor, currency ON products
    FOR EACH ROW EXECUTE FUNCTION products_sync_legacy_price();

CREATE FUNCTION orders_fill_legacy_columns() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.product_id IS NOT NULL THEN
        NEW.currency := coalesce(NEW.currency, 'RUB');
        NEW.total_minor := coalesce(NEW.total_minor, NEW.price::BIGINT * 100);
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER orders_fill_legacy_columns
    BEFORE INSERT ON orders
    FOR EACH ROW EXECUTE FUNCTION orders_fill_legacy_columns();

CREATE FUNCTION orders_create_legacy_item() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.product_id IS NOT NULL THEN
        INSERT INTO order_items (
            order_id, product_id, product_name, product_type, fulfillment_type,
            quantity, unit_price_minor, currency, line_total_minor
        )
        SELECT NEW.id, p.id, p.name, p.product_type, p.fulfillment_type,
               1, NEW.price::BIGINT * 100, 'RUB', NEW.price::BIGINT * 100
        FROM products p
        WHERE p.id = NEW.product_id;
    END IF;
    RETURN NULL;
END
$$;

CREATE TRIGGER orders_create_legacy_item
    AFTER INSERT ON orders
    FOR EACH ROW EXECUTE FUNCTION orders_create_legacy_item();

CREATE FUNCTION orders_protect_history() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.product_id IS DISTINCT FROM OLD.product_id
       OR NEW.price IS DISTINCT FROM OLD.price
       OR NEW.currency IS DISTINCT FROM OLD.currency
       OR NEW.total_minor IS DISTINCT FROM OLD.total_minor THEN
        RAISE EXCEPTION 'сумма, валюта и товар заказа % неизменяемы', OLD.id;
    END IF;
    IF OLD.items_sealed AND NOT NEW.items_sealed THEN
        RAISE EXCEPTION 'состав заказа % закрыт, открыть его нельзя', OLD.id;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER orders_protect_history
    BEFORE UPDATE OF product_id, price, currency, total_minor, items_sealed ON orders
    FOR EACH ROW EXECUTE FUNCTION orders_protect_history();

CREATE FUNCTION orders_guard_status() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.status NOT IN ('pending', 'legacy') THEN
            RAISE EXCEPTION 'новый заказ создаётся в статусе pending, а не %', NEW.status;
        END IF;
        IF NEW.paid_at IS NOT NULL THEN
            RAISE EXCEPTION 'paid_at задаётся только при подтверждении оплаты';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF NOT (
               (OLD.status = 'pending' AND NEW.status IN ('paid', 'requires_action', 'cancelled'))
            OR (OLD.status = 'requires_action' AND NEW.status IN ('paid', 'cancelled'))
        ) THEN
            RAISE EXCEPTION 'переход статуса заказа % из % в % запрещён', OLD.id, OLD.status, NEW.status;
        END IF;
        IF NEW.status = 'paid' AND NEW.paid_at IS NULL THEN
            RAISE EXCEPTION 'при подтверждении оплаты заказа % нужно paid_at', OLD.id;
        END IF;
    END IF;

    IF NEW.paid_at IS DISTINCT FROM OLD.paid_at
       AND NOT (NEW.status = 'paid' AND OLD.status IS DISTINCT FROM 'paid') THEN
        RAISE EXCEPTION 'paid_at заказа % задаётся только при подтверждении оплаты и не меняется', OLD.id;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER orders_guard_status
    BEFORE INSERT OR UPDATE OF status, paid_at ON orders
    FOR EACH ROW EXECUTE FUNCTION orders_guard_status();

CREATE FUNCTION orders_protect_delete() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('digital_store.allow_history_delete', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'заказы не удаляются (заказ %); для отмены используйте статус', OLD.id;
END
$$;

CREATE TRIGGER orders_protect_delete
    BEFORE DELETE ON orders
    FOR EACH ROW EXECUTE FUNCTION orders_protect_delete();

CREATE FUNCTION order_items_protect_history() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('digital_store.allow_history_delete', true) = 'on' THEN
        RETURN OLD;  -- целостность оставшегося заказа проверит order_items_check_after_delete при COMMIT
    END IF;
    RAISE EXCEPTION 'позиции заказа неизменяемы и не удаляются (позиция %)', OLD.id;
END
$$;

CREATE TRIGGER order_items_protect_history
    BEFORE UPDATE OR DELETE ON order_items
    FOR EACH ROW EXECUTE FUNCTION order_items_protect_history();

CREATE FUNCTION order_history_protect_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'TRUNCATE % запрещён: история заказов удаляется только DELETE с проверкой состава', TG_TABLE_NAME;
END
$$;

CREATE TRIGGER orders_protect_truncate
    BEFORE TRUNCATE ON orders
    FOR EACH STATEMENT EXECUTE FUNCTION order_history_protect_truncate();

CREATE TRIGGER order_items_protect_truncate
    BEFORE TRUNCATE ON order_items
    FOR EACH STATEMENT EXECUTE FUNCTION order_history_protect_truncate();

CREATE FUNCTION order_items_guard_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    sealed BOOLEAN;
    order_currency TEXT;
BEGIN
    SELECT items_sealed, currency INTO sealed, order_currency
    FROM orders
    WHERE id = NEW.order_id;

    IF NOT FOUND THEN
        RETURN NEW;  -- несуществующий заказ отклонит внешний ключ
    END IF;
    IF sealed THEN
        RAISE EXCEPTION 'состав заказа % уже зафиксирован: позиции добавляются только при создании заказа', NEW.order_id;
    END IF;
    IF NEW.currency <> order_currency THEN
        RAISE EXCEPTION 'валюта позиции (%) не совпадает с валютой заказа % (%)', NEW.currency, NEW.order_id, order_currency;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER order_items_guard_insert
    BEFORE INSERT ON order_items
    FOR EACH ROW EXECUTE FUNCTION order_items_guard_insert();

CREATE FUNCTION orders_check_composition() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    checked_order_id INTEGER;
    order_total BIGINT;
    order_currency TEXT;
    items_count BIGINT;
    items_total NUMERIC;
    foreign_currency BIGINT;
BEGIN
    IF TG_TABLE_NAME = 'orders' THEN
        checked_order_id := NEW.id;
    ELSIF TG_OP = 'DELETE' THEN
        checked_order_id := OLD.order_id;
    ELSE
        checked_order_id := NEW.order_id;
    END IF;

    SELECT total_minor, currency INTO order_total, order_currency
    FROM orders
    WHERE id = checked_order_id;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT count(*), coalesce(sum(line_total_minor), 0), count(*) FILTER (WHERE currency <> order_currency)
    INTO items_count, items_total, foreign_currency
    FROM order_items
    WHERE order_id = checked_order_id;

    IF items_count = 0 THEN
        RAISE EXCEPTION 'заказ % не может быть без позиций', checked_order_id;
    END IF;
    IF items_total <> order_total THEN
        RAISE EXCEPTION 'итог заказа % (%) не равен сумме позиций (%)', checked_order_id, order_total, items_total;
    END IF;
    IF foreign_currency > 0 THEN
        RAISE EXCEPTION 'в заказе % есть позиции в другой валюте', checked_order_id;
    END IF;

    IF TG_TABLE_NAME = 'orders' THEN
        UPDATE orders SET items_sealed = TRUE WHERE id = checked_order_id AND NOT items_sealed;
    END IF;
    RETURN NULL;
END
$$;

CREATE CONSTRAINT TRIGGER orders_check_composition
    AFTER INSERT ON orders
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION orders_check_composition();

CREATE CONSTRAINT TRIGGER order_items_check_composition
    AFTER INSERT ON order_items
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION orders_check_composition();

CREATE CONSTRAINT TRIGGER order_items_check_after_delete
    AFTER DELETE ON order_items
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION orders_check_composition();

-- ---------- 5б. доверенный search_path триггерных функций ----------
--
-- Функции обращаются к таблицам без имени схемы. PostgreSQL ищет временную схему
-- сессии (pg_temp) ПЕРВОЙ, если pg_temp не указан в пути явно. Без этого блока любая
-- сессия могла бы создать CREATE TEMP TABLE orders / order_items / products и подменить
-- данные, которые видят проверки (обход закрытия состава, проверки итога, снимка товара).
--
-- Поэтому каждой функции задаётся путь: pg_catalog (встроенные функции нельзя подменить),
-- схема магазина (current_schema() при миграции: public в рабочей базе, ds_mig_* в тестах),
-- pg_temp — последней. Значение хранится в функции буквально и переносится pg_dump.
-- schema.sql содержит этот же блок.

DO $$
DECLARE
    shop_schema TEXT := current_schema();
    fn TEXT;
BEGIN
    IF shop_schema IS NULL OR shop_schema LIKE 'pg\_temp%' OR shop_schema = 'pg_catalog' THEN
        RAISE EXCEPTION 'миграция должна выполняться со схемой магазина в search_path (сейчас: %)', shop_schema;
    END IF;
    FOREACH fn IN ARRAY ARRAY[
        'products_sync_legacy_price',
        'orders_fill_legacy_columns',
        'orders_create_legacy_item',
        'orders_protect_history',
        'orders_guard_status',
        'orders_protect_delete',
        'order_items_protect_history',
        'order_history_protect_truncate',
        'order_items_guard_insert',
        'orders_check_composition'
    ] LOOP
        EXECUTE format('ALTER FUNCTION %I.%I() SET search_path = pg_catalog, %I, pg_temp',
                       shop_schema, fn, shop_schema);
    END LOOP;
END
$$;
