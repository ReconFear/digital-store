-- 0002 — универсальное коммерческое ядро (PR 2).
--
-- Что делает:
--   1. products: точные деньги (price_minor BIGINT + currency) и три независимых признака:
--        product_type     — что продаётся (digital_key, digital_file, physical, service, other);
--        inventory_mode   — учитывается ли остаток (tracked) или нет (unlimited);
--        fulfillment_type — какой модуль исполняет заказ после оплаты
--                           (manual, digital_keys, downloads, physical_shipping).
--   2. orders: валюта и итог заказа в минимальных единицах, created_at/paid_at,
--      статус requires_action. Старые колонки product_id/price сохраняются для
--      заказов старого формата (v1) и становятся NULL у многопозиционных заказов (v2).
--   3. order_items: позиции заказа со снимком названия, типа, цены и итога строки.
--   4. fulfillments: состояние исполнения каждой оплаченной позиции.
--   5. Слой совместимости: старые INSERT в products (только price) и в orders
--      (product_id, price) продолжают работать — триггеры заполняют новые поля
--      и создают позицию заказа.
--   6. Целостность заказа (раздел 5а): состав заказа записывается только до COMMIT
--      транзакции, создавшей заказ; при COMMIT проверяется состав и заказ закрывается
--      (items_sealed = TRUE, снять нельзя); позиции и заказы нельзя изменить; удаление —
--      только осознанно и только так, чтобы не осталось заказа с неверным составом;
--      TRUNCATE запрещён. Статусы меняются только по разрешённым переходам.
--
-- ПЕРЕНОС СТАРЫХ СУММ. Старые products.price и orders.price — целые рубли
-- (подтверждается кодом и документацией проекта: вывод "{price} RUB", FRONTEND_API.md).
-- Поэтому price_minor = price * 100 копеек, currency = 'RUB'. Преобразование
-- однозначно для любых значений, допустимых старой схемой (INTEGER >= 0, NOT NULL),
-- и проверяется в конце миграции. Если хоть одна сумма или позиция не сходится —
-- миграция падает, migrate.py откатывает весь запуск.
--
-- Старые товары: product_type = 'other', inventory_mode = 'tracked',
-- fulfillment_type = 'manual'. Тип не угадывается по названию; поведение не меняется
-- (остаток учитывается как раньше, автоматической выдачи нет). Переклассифицировать
-- товары можно позже обычным UPDATE.
--
-- Старые заказы: одна позиция (quantity = 1, цена = orders.price), название и тип —
-- по текущему состоянию товара (историческое название в старой схеме не хранилось).
-- created_at старых заказов остаётся NULL: время их создания неизвестно.
-- Строки fulfillments для старых оплаченных заказов не создаются: их исполнение
-- в старой системе не отслеживалось, и придумывать его состояние нельзя.

-- ---------- 1. products ----------

ALTER TABLE products
    ADD COLUMN price_minor BIGINT,
    ADD COLUMN currency TEXT NOT NULL DEFAULT 'RUB',
    ADD COLUMN product_type TEXT NOT NULL DEFAULT 'other',
    ADD COLUMN inventory_mode TEXT NOT NULL DEFAULT 'tracked',
    ADD COLUMN fulfillment_type TEXT NOT NULL DEFAULT 'manual';

UPDATE products SET price_minor = price::BIGINT * 100;

ALTER TABLE products
    ALTER COLUMN price_minor SET NOT NULL,
    ALTER COLUMN price DROP NOT NULL,
    ADD CONSTRAINT products_price_minor_check
        CHECK (price_minor >= 0 AND price_minor <= 1000000000000),
    ADD CONSTRAINT products_currency_check
        CHECK (currency ~ '^[A-Z]{3}$'),
    ADD CONSTRAINT products_legacy_price_check
        CHECK (price IS NULL OR (currency = 'RUB' AND price_minor = price::BIGINT * 100)),
    ADD CONSTRAINT products_product_type_check
        CHECK (product_type IN ('digital_key', 'digital_file', 'physical', 'service', 'other')),
    ADD CONSTRAINT products_inventory_mode_check
        CHECK (inventory_mode IN ('tracked', 'unlimited')),
    ADD CONSTRAINT products_fulfillment_type_check
        CHECK (fulfillment_type IN ('manual', 'digital_keys', 'downloads', 'physical_shipping')),
    ADD CONSTRAINT products_type_combination_check CHECK (
           (product_type = 'digital_key'  AND inventory_mode = 'tracked' AND fulfillment_type IN ('digital_keys', 'manual'))
        OR (product_type = 'digital_file' AND fulfillment_type IN ('downloads', 'manual'))
        OR (product_type = 'physical'     AND inventory_mode = 'tracked' AND fulfillment_type IN ('physical_shipping', 'manual'))
        OR (product_type = 'service'      AND fulfillment_type = 'manual')
        OR (product_type = 'other'        AND fulfillment_type = 'manual')
    ),
    ADD CONSTRAINT products_unlimited_quantity_check
        CHECK (inventory_mode = 'tracked' OR quantity = 0);

-- price_minor — главная цена. Устаревший price (целые рубли) поддерживается автоматически:
--   * задан только price (старый INSERT/UPDATE) -> price_minor = price * 100, только для RUB;
--   * задан price_minor -> price = price_minor / 100, если это RUB и целые рубли, иначе NULL.
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

-- ---------- 2. orders ----------

-- created_at добавляется без DEFAULT, чтобы старые заказы не получили время миграции.
-- items_sealed — состав заказа закрыт. Ставится автоматически при COMMIT транзакции,
-- создавшей заказ (orders_check_composition), снять нельзя. Это обычные данные:
-- значение переживает pg_dump/pg_restore и не зависит от номеров транзакций кластера.
ALTER TABLE orders
    ADD COLUMN currency TEXT,
    ADD COLUMN total_minor BIGINT,
    ADD COLUMN created_at TIMESTAMPTZ,
    ADD COLUMN paid_at TIMESTAMPTZ,
    ADD COLUMN items_sealed BOOLEAN NOT NULL DEFAULT FALSE;

UPDATE orders SET currency = 'RUB', total_minor = price::BIGINT * 100;

-- Ограничение статуса заменяется по определению, а не по имени (имя могло отличаться
-- в базах, где 0001 применяли вручную; 0000/0001 проверили только его определение).
DO $$
DECLARE
    old_name TEXT;
BEGIN
    SELECT conname INTO old_name
    FROM pg_constraint
    WHERE conrelid = to_regclass('orders')
      AND contype = 'c'
      AND pg_get_constraintdef(oid) =
          'CHECK ((status = ANY (ARRAY[''pending''::text, ''paid''::text, ''cancelled''::text, ''legacy''::text])))';
    IF old_name IS NULL THEN
        RAISE EXCEPTION 'не найдено ожидаемое ограничение статуса orders (из 0001)';
    END IF;
    EXECUTE format('ALTER TABLE orders DROP CONSTRAINT %I', old_name);
END
$$;

ALTER TABLE orders
    ALTER COLUMN created_at SET DEFAULT now(),
    ALTER COLUMN currency SET NOT NULL,
    ALTER COLUMN total_minor SET NOT NULL,
    ALTER COLUMN product_id DROP NOT NULL,
    ALTER COLUMN price DROP NOT NULL,
    ADD CONSTRAINT orders_status_check
        CHECK (status IN ('pending', 'paid', 'cancelled', 'legacy', 'requires_action')),
    ADD CONSTRAINT orders_currency_check
        CHECK (currency ~ '^[A-Z]{3}$'),
    ADD CONSTRAINT orders_total_minor_check
        CHECK (total_minor >= 0),
    ADD CONSTRAINT orders_legacy_shape_check
        CHECK ((product_id IS NULL) = (price IS NULL)),
    ADD CONSTRAINT orders_legacy_total_check
        CHECK (product_id IS NULL OR (currency = 'RUB' AND total_minor = price::BIGINT * 100)),
    ADD CONSTRAINT orders_paid_at_check
        CHECK (paid_at IS NULL OR status = 'paid');

-- ---------- 3. order_items ----------

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

-- Перенос: одна позиция на каждый существующий заказ.
INSERT INTO order_items (
    order_id, product_id, product_name, product_type, fulfillment_type,
    quantity, unit_price_minor, currency, line_total_minor
)
SELECT o.id, p.id, p.name, p.product_type, p.fulfillment_type,
       1, o.price::BIGINT * 100, 'RUB', o.price::BIGINT * 100
FROM orders o
JOIN products p ON p.id = o.product_id
ORDER BY o.id;

-- Состав перенесённых заказов закрыт.
UPDATE orders SET items_sealed = TRUE;

-- ---------- 4. fulfillments ----------

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

-- Путь поиска всех триггерных функций закрепляется в конце раздела 5а
-- (pg_catalog, схема магазина, pg_temp) — см. «доверенный search_path».

-- ---------- 5. совместимость со старыми INSERT ----------

-- Старый INSERT INTO orders (product_id, price, ...) получает валюту и итог...
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

-- ...и позицию заказа со снимком товара.
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

-- ---------- 5а. целостность заказа и статусы ----------
--
-- Транзакционные границы:
--   * заказ и весь его состав создаются в ОДНОЙ транзакции (v1: INSERT orders + триггер;
--     v2: commerce/orders.py; миграция: перенос в транзакции migrate.py);
--   * пока items_sealed = FALSE (до COMMIT этой транзакции), позиции добавлять можно
--     (order_items_guard_insert); другие транзакции незакоммиченный заказ не видят;
--   * при COMMIT отложенная проверка orders_check_composition убеждается, что позиции есть,
--     сумма line_total_minor = total_minor и валюта одна, и ставит items_sealed = TRUE;
--   * после этого состав, суммы, валюта и товар неизменяемы; items_sealed снять нельзя.
--
-- Удаление истории (уборка тестовых данных, ручное исправление) — только осознанно:
--     BEGIN; SET LOCAL digital_store.allow_history_delete = 'on'; DELETE ...; COMMIT;
-- При COMMIT отложенная проверка на DELETE не даст оставить заказ без позиций или с
-- неверным итогом: удалять можно только заказ целиком (позиции + заказ в одной транзакции).
-- TRUNCATE запрещён всегда: он не вызывает построчных проверок.
-- Это защита от ошибок приложения и случайного SQL, а не от владельца базы
-- (тот может отключить триггер). Разграничение прав — средствами PostgreSQL.
--
-- Статусы (orders_guard_status):
--   новый заказ: pending (или legacy — инертная историческая запись);
--   pending         -> paid | requires_action | cancelled
--   requires_action -> paid | cancelled
--   paid, cancelled, legacy — конечные (возвраты и отмены оплаченных — следующий этап);
--   paid_at задаётся только при переходе в paid и потом не меняется; у исторических
--   оплаченных заказов остаётся NULL (время неизвестно).

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

-- Позицию можно добавить только в незакрытый заказ (то есть до COMMIT транзакции,
-- создавшей заказ) и только в валюте заказа.
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

-- При COMMIT: у существующего заказа есть позиции, сумма сходится, валюта одна.
-- Для события INSERT в orders заказ после проверки закрывается (items_sealed = TRUE).
-- Если заказ к моменту COMMIT удалён (осознанная уборка целиком) — проверять нечего.
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

-- ---------- 6. проверка переноса ----------

DO $$
DECLARE
    problems TEXT[] := ARRAY[]::TEXT[];
    n BIGINT;
BEGIN
    SELECT count(*) INTO n FROM products WHERE price IS NULL OR price_minor <> price::BIGINT * 100 OR currency <> 'RUB';
    IF n > 0 THEN
        problems := problems || format('%s товаров с несошедшейся ценой', n);
    END IF;

    SELECT count(*) INTO n
    FROM orders o
    WHERE (SELECT count(*) FROM order_items i WHERE i.order_id = o.id) <> 1;
    IF n > 0 THEN
        problems := problems || format('%s заказов без ровно одной позиции', n);
    END IF;

    SELECT count(*) INTO n
    FROM orders o
    WHERE o.total_minor <> (SELECT sum(i.line_total_minor) FROM order_items i WHERE i.order_id = o.id)
       OR o.total_minor <> o.price::BIGINT * 100;
    IF n > 0 THEN
        problems := problems || format('%s заказов с несошедшимся итогом', n);
    END IF;

    IF cardinality(problems) > 0 THEN
        RAISE EXCEPTION 'Перенос данных в 0002 не сошёлся: %', array_to_string(problems, '; ');
    END IF;
END
$$;
