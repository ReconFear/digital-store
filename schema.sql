SET client_encoding = 'UTF8';

-- Полная текущая схема Digital Store — справочный снимок (после миграции 0005).
-- Для 0005 нужен pgcrypto в схеме ext_pgcrypto (DB_ROLES.md, шаг 4а).
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

-- ================= платежи и безопасность (миграция 0003) =================

-- 0003 — платежи и безопасность (PR 3, rev2).
--
-- Что делает:
--   1. order_access      — секрет доступа к заказу (хранится только SHA-256), выдаётся при создании
--                          заказа v2; без него заказ нельзя посмотреть или оплатить по HTTP.
--   2. payments          — платёжные попытки у провайдера (ключ идемпотентности, сумма, статус).
--   3. money_operations  — подтверждённые денежные операции (успешная оплата, возврат). Неизменяемы.
--   4. provider_events   — входящие уведомления провайдера: долговременная очередь с арендой
--                          (lease_token) для обработчиков; тело уведомления не хранится.
--   5. refunds           — запросы на возврат (исполнение у провайдера — следующий этап).
--   6. audit_log         — журнал значимых действий (только добавление).
--   7. apply_paid_order() — списание остатков, перевод в paid, задачи исполнения. Это перенос
--      логики payment_service.apply_verified_payment в SQL без изменения поведения, чтобы её
--      можно было вызывать из SECURITY DEFINER-функции в одной транзакции с фиксацией платежа.
--   8. SECURITY DEFINER-функции: веб-приложению — только функции, требующие секрет заказа;
--      обработчику — учёт проверенного состояния; администратору — разрешение спорных случаев.
--
-- Деньги — BIGINT в минимальных единицах, валюта явно. Роли PostgreSQL миграция НЕ создаёт и
-- права НЕ выдаёт: SQL для ролей формирует deploy/db_roles.py, применяет человек (DB_ROLES.md).
-- Все функции получают путь поиска pg_catalog, <схема>, pg_temp (раздел 10), EXECUTE у PUBLIC
-- отозван у всех функций, которые вызываются напрямую.

-- ---------- 1. доступ к заказу ----------

CREATE TABLE order_access (
    order_id INTEGER PRIMARY KEY REFERENCES orders(id),
    token_sha256 BYTEA NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT order_access_token_sha256_check CHECK (octet_length(token_sha256) = 32),
    CONSTRAINT order_access_token_key UNIQUE (token_sha256)
);

-- ---------- 2. платёжные попытки ----------
--
-- Статусы (payments_guard):
--   creating            запись создана, ключ идемпотентности сохранён ДО запроса к провайдеру
--   unknown             запрос к провайдеру ушёл, ответ неизвестен (таймаут, 5xx) — выяснить сверкой
--   pending             платёж создан у провайдера, ждёт покупателя
--   waiting_for_capture деньги заблокированы (при capture=true не ожидается, но обрабатывается)
--   succeeded           провайдер подтвердил оплату, суммы сверены — конечный
--   canceled            провайдер отменил платёж — конечный
--   failed              провайдер отклонил создание (4xx), платежа у него нет — конечный
--   requires_action     автоматически решить нельзя — нужен человек. Пока не разрешено
--                       (resolved_at IS NULL), новые попытки оплаты заказа НЕ создаются.
--                       Причина unresolved_attempt (результат создания неизвестен дольше, чем
--                       живёт ключ идемпотентности) — не окончательна: если платёж найдётся у
--                       провайдера, попытка продолжит обычную обработку.
--
-- resolution (только для requires_action, ставит администратор через admin_resolve_payment):
--   not_paid_confirmed  человек проверил у провайдера: денег по попытке нет и не будет
--   handled_manually    вопрос решён вручную (например, возврат сделан в кабинете провайдера)

CREATE TABLE payments (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    provider TEXT NOT NULL,
    idempotence_key UUID NOT NULL,
    provider_payment_id TEXT,
    amount_minor BIGINT NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'creating',
    status_reason TEXT,
    confirmation_url TEXT,
    provider_test BOOLEAN,
    resolution TEXT,
    resolved_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_checked_at TIMESTAMPTZ,
    CONSTRAINT payments_provider_check CHECK (provider ~ '^[a-z][a-z0-9_]{1,31}$'),
    CONSTRAINT payments_idempotence_key_key UNIQUE (idempotence_key),
    CONSTRAINT payments_provider_payment_id_check CHECK (provider_payment_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    CONSTRAINT payments_provider_payment_key UNIQUE (provider, provider_payment_id),
    CONSTRAINT payments_amount_minor_check CHECK (amount_minor > 0 AND amount_minor <= 1000000000000000),
    CONSTRAINT payments_currency_check CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT payments_status_check CHECK (status IN (
        'creating', 'unknown', 'pending', 'waiting_for_capture',
        'succeeded', 'canceled', 'failed', 'requires_action')),
    CONSTRAINT payments_status_reason_check CHECK (char_length(status_reason) <= 100),
    CONSTRAINT payments_confirmation_url_check CHECK (char_length(confirmation_url) <= 2048),
    CONSTRAINT payments_provider_id_required_check CHECK (
        status IN ('creating', 'unknown', 'failed', 'requires_action') OR provider_payment_id IS NOT NULL),
    CONSTRAINT payments_resolution_check CHECK (
        (resolution IS NULL AND resolved_at IS NULL)
        OR (status = 'requires_action' AND resolved_at IS NOT NULL
            AND resolution IN ('not_paid_confirmed', 'handled_manually')))
);

CREATE INDEX payments_order_id_idx ON payments (order_id);
-- Не больше одной успешной оплаты на заказ и одной незавершённой попытки на заказ.
CREATE UNIQUE INDEX payments_one_succeeded_per_order ON payments (order_id) WHERE status = 'succeeded';
CREATE UNIQUE INDEX payments_one_active_per_order ON payments (order_id)
    WHERE status IN ('creating', 'unknown', 'pending', 'waiting_for_capture');
CREATE INDEX payments_open_idx ON payments (updated_at)
    WHERE status IN ('creating', 'unknown', 'pending', 'waiting_for_capture');
CREATE INDEX payments_unresolved_idx ON payments (order_id)
    WHERE status = 'requires_action' AND resolved_at IS NULL;

-- ---------- 3. подтверждённые денежные операции ----------

CREATE TABLE money_operations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind TEXT NOT NULL,
    payment_id BIGINT NOT NULL REFERENCES payments(id),
    order_id INTEGER NOT NULL REFERENCES orders(id),
    provider TEXT NOT NULL,
    provider_operation_id TEXT NOT NULL,
    amount_minor BIGINT NOT NULL,
    currency TEXT NOT NULL,
    provider_test BOOLEAN NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT money_operations_kind_check CHECK (kind IN ('payment', 'refund')),
    CONSTRAINT money_operations_amount_minor_check CHECK (amount_minor > 0 AND amount_minor <= 1000000000000000),
    CONSTRAINT money_operations_currency_check CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT money_operations_provider_operation_key UNIQUE (provider, kind, provider_operation_id)
);

CREATE UNIQUE INDEX money_operations_one_payment_per_attempt ON money_operations (payment_id) WHERE kind = 'payment';
CREATE INDEX money_operations_order_id_idx ON money_operations (order_id);

-- ---------- 4. входящие уведомления провайдера ----------
-- Тело уведомления не хранится: ему не доверяем, данные платежа всегда запрашиваются у провайдера.
--
-- Протокол аренды (обработчик — payments/service.py):
--   received/failed --захват--> processing (новый lease_token, lease_expires_at)
--   processing --завершение с ТЕМ ЖЕ lease_token--> processed | ignored | failed | received*
--   processing с истёкшей арендой снова доступна для захвата (новый lease_token);
--   завершение устаревшим обработчиком (чужой/старый lease_token) ничего не меняет.
-- Повторная доставка во время обработки не трогает аренду, а ставит recheck_requested:
-- после завершения событие вернётся в очередь (* received) и будет перепроверено.

CREATE TABLE provider_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider TEXT NOT NULL,
    event_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    deliveries INTEGER NOT NULL DEFAULT 1,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    recheck_requested BOOLEAN NOT NULL DEFAULT FALSE,
    CONSTRAINT provider_events_provider_check CHECK (provider ~ '^[a-z][a-z0-9_]{1,31}$'),
    CONSTRAINT provider_events_event_type_check CHECK (event_type ~ '^[a-z_.]{1,64}$'),
    CONSTRAINT provider_events_object_id_check CHECK (object_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    CONSTRAINT provider_events_status_check CHECK (status IN ('received', 'processing', 'processed', 'ignored', 'failed')),
    CONSTRAINT provider_events_counters_check CHECK (deliveries >= 1 AND attempts >= 0),
    CONSTRAINT provider_events_last_error_check CHECK (char_length(last_error) <= 200),
    CONSTRAINT provider_events_lease_check CHECK (
        (status = 'processing' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (status <> 'processing' AND lease_token IS NULL AND lease_expires_at IS NULL)),
    CONSTRAINT provider_events_key UNIQUE (provider, event_type, object_id)
);

CREATE INDEX provider_events_due_idx ON provider_events (next_attempt_at) WHERE status IN ('received', 'failed');
CREATE INDEX provider_events_lease_idx ON provider_events (lease_expires_at) WHERE status = 'processing';

-- ---------- 5. возвраты ----------
-- requested — нужен возврат (запрошен человеком или автоматически при повторной оплате).
-- Исполнение возврата у провайдера в PR 3 НЕ реализовано: статус не меняется сам.

CREATE TABLE refunds (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    payment_id BIGINT NOT NULL REFERENCES payments(id),
    order_id INTEGER NOT NULL REFERENCES orders(id),
    amount_minor BIGINT NOT NULL,
    currency TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested',
    reason TEXT NOT NULL,
    idempotence_key UUID NOT NULL DEFAULT gen_random_uuid(),
    provider_refund_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT refunds_amount_minor_check CHECK (amount_minor > 0 AND amount_minor <= 1000000000000000),
    CONSTRAINT refunds_currency_check CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT refunds_status_check CHECK (status IN ('requested', 'pending', 'succeeded', 'canceled', 'requires_action')),
    CONSTRAINT refunds_reason_check CHECK (char_length(reason) BETWEEN 1 AND 200),
    CONSTRAINT refunds_idempotence_key_key UNIQUE (idempotence_key),
    CONSTRAINT refunds_provider_refund_id_check CHECK (provider_refund_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    CONSTRAINT refunds_provider_refund_key UNIQUE (provider_refund_id)
);

CREATE INDEX refunds_payment_id_idx ON refunds (payment_id);

-- ---------- 6. журнал ----------

CREATE TABLE audit_log (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    order_id INTEGER,
    payment_id BIGINT,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT audit_log_actor_check CHECK (actor ~ '^[a-z_]{1,32}$'),
    CONSTRAINT audit_log_action_check CHECK (action ~ '^[a-z_.]{1,64}$'),
    CONSTRAINT audit_log_details_check CHECK (jsonb_typeof(details) = 'object' AND octet_length(details::text) <= 2000)
);

CREATE INDEX audit_log_order_id_idx ON audit_log (order_id);

-- ---------- 7. защитные триггеры ----------

-- Платёжная попытка: создаётся только как 'creating' на точную сумму заказа в статусе pending;
-- сумма, валюта, заказ, провайдер и ключ неизменяемы; ID у провайдера задаётся один раз;
-- статусы — только по разрешённым переходам.
CREATE FUNCTION payments_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    order_total BIGINT;
    order_currency TEXT;
    order_status TEXT;
BEGIN
    IF TG_OP = 'INSERT' THEN
        SELECT total_minor, currency, status INTO order_total, order_currency, order_status
        FROM orders WHERE id = NEW.order_id;
        IF NOT FOUND THEN
            RETURN NEW;  -- внешний ключ отклонит
        END IF;
        IF NEW.status <> 'creating' OR NEW.provider_payment_id IS NOT NULL OR NEW.resolution IS NOT NULL THEN
            RAISE EXCEPTION 'платёжная попытка создаётся только в статусе creating';
        END IF;
        IF order_status <> 'pending' THEN
            RAISE EXCEPTION 'заказ % в статусе % нельзя оплачивать', NEW.order_id, order_status;
        END IF;
        IF NEW.amount_minor <> order_total OR NEW.currency <> order_currency THEN
            RAISE EXCEPTION 'сумма платежа должна быть равна итогу заказа %', NEW.order_id;
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.order_id <> OLD.order_id OR NEW.provider <> OLD.provider
       OR NEW.idempotence_key <> OLD.idempotence_key
       OR NEW.amount_minor <> OLD.amount_minor OR NEW.currency <> OLD.currency
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'заказ, сумма, валюта, провайдер и ключ платежа % неизменяемы', OLD.id;
    END IF;
    IF OLD.provider_payment_id IS NOT NULL AND NEW.provider_payment_id IS DISTINCT FROM OLD.provider_payment_id THEN
        RAISE EXCEPTION 'ID платежа % у провайдера уже задан и не меняется', OLD.id;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
           (OLD.status IN ('creating', 'unknown')
                AND NEW.status IN ('unknown', 'pending', 'waiting_for_capture', 'succeeded', 'canceled', 'failed', 'requires_action'))
        OR (OLD.status = 'pending' AND NEW.status IN ('waiting_for_capture', 'succeeded', 'canceled', 'requires_action'))
        OR (OLD.status = 'waiting_for_capture' AND NEW.status IN ('succeeded', 'canceled', 'requires_action'))
        -- неразрешённая «потерянная» попытка нашлась у провайдера — обычная обработка продолжается
        OR (OLD.status = 'requires_action' AND OLD.status_reason = 'unresolved_attempt' AND OLD.resolved_at IS NULL
                AND NEW.status IN ('pending', 'waiting_for_capture', 'succeeded', 'canceled'))
    ) THEN
        RAISE EXCEPTION 'переход статуса платежа % из % в % запрещён', OLD.id, OLD.status, NEW.status;
    END IF;
    -- «Потерянная» попытка становится ожидающей оплаты только вместе со ссылкой оплаты:
    -- иначе покупатель получил бы awaiting_payment, которым нельзя воспользоваться.
    IF OLD.status = 'requires_action' AND NEW.status = 'pending'
       AND (NEW.confirmation_url IS NULL OR NEW.confirmation_url !~ '^https://[^/@[:space:]]+/') THEN
        RAISE EXCEPTION 'попытка % не может ожидать оплаты без https-ссылки оплаты', OLD.id;
    END IF;
    -- succeeded только вместе с учтённой денежной операцией (её пишет payment_record_provider_state).
    IF NEW.status = 'succeeded' AND OLD.status <> 'succeeded' AND NOT EXISTS (
        SELECT 1 FROM money_operations WHERE payment_id = NEW.id AND kind = 'payment'
           AND amount_minor = NEW.amount_minor AND currency = NEW.currency) THEN
        RAISE EXCEPTION 'платёж % нельзя отметить успешным без учтённой денежной операции на ту же сумму', OLD.id;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

CREATE TRIGGER payments_guard
    BEFORE INSERT OR UPDATE ON payments
    FOR EACH ROW EXECUTE FUNCTION payments_guard();

-- Возврат относится только к учтённым деньгам: та же валюта и тот же заказ, что в
-- money_operations по этому платежу, и в сумме (без отменённых) не больше полученного.
-- Это действует для всех путей создания возврата: автоматических и request_refund.
CREATE FUNCTION refunds_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_received RECORD;
    v_requested BIGINT;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.payment_id <> OLD.payment_id OR NEW.order_id <> OLD.order_id
           OR NEW.amount_minor <> OLD.amount_minor OR NEW.currency <> OLD.currency
           OR NEW.created_at <> OLD.created_at OR NEW.idempotence_key <> OLD.idempotence_key THEN
            RAISE EXCEPTION 'платёж, заказ, сумма и валюта возврата % неизменяемы', OLD.id;
        END IF;
        NEW.updated_at := now();
        IF NOT (OLD.status = 'canceled' AND NEW.status <> 'canceled') THEN
            RETURN NEW;
        END IF;
    END IF;

    PERFORM 1 FROM payments WHERE id = NEW.payment_id FOR UPDATE;   -- параллельные запросы по очереди
    SELECT order_id, amount_minor, currency INTO v_received
    FROM money_operations WHERE payment_id = NEW.payment_id AND kind = 'payment';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'по платежу % деньги не учтены — возвращать нечего', NEW.payment_id;
    END IF;
    IF NEW.currency <> v_received.currency OR NEW.order_id <> v_received.order_id THEN
        RAISE EXCEPTION 'возврат должен быть в валюте учтённой оплаты (%) и по её заказу', v_received.currency;
    END IF;
    SELECT coalesce(sum(amount_minor), 0) INTO v_requested
    FROM refunds WHERE payment_id = NEW.payment_id AND status <> 'canceled' AND id <> NEW.id;
    IF v_requested + NEW.amount_minor > v_received.amount_minor THEN
        RAISE EXCEPTION 'сумма возврата больше доступной (получено % %, уже запрошено %)',
            v_received.amount_minor, v_received.currency, v_requested;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER refunds_guard
    BEFORE INSERT OR UPDATE ON refunds
    FOR EACH ROW EXECUTE FUNCTION refunds_guard();

-- Финансовая история: без изменения; удаление — только осознанно (уборка тестовых данных).
CREATE FUNCTION financial_history_protect() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('digital_store.allow_history_delete', true) = 'on' THEN
        RETURN OLD;
    END IF;
    IF TG_OP = 'UPDATE' AND TG_TABLE_NAME IN ('payments', 'refunds') THEN
        RETURN NEW;  -- изменения платежей и возвратов охраняют их собственные правила
    END IF;
    RAISE EXCEPTION '% запрещён для %: финансовая история неизменяема', TG_OP, TG_TABLE_NAME;
END
$$;

CREATE TRIGGER money_operations_protect
    BEFORE UPDATE OR DELETE ON money_operations
    FOR EACH ROW EXECUTE FUNCTION financial_history_protect();

CREATE TRIGGER audit_log_protect
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION financial_history_protect();

CREATE TRIGGER payments_protect_delete
    BEFORE DELETE ON payments
    FOR EACH ROW EXECUTE FUNCTION financial_history_protect();

CREATE TRIGGER refunds_protect_delete
    BEFORE DELETE ON refunds
    FOR EACH ROW EXECUTE FUNCTION financial_history_protect();

CREATE TRIGGER order_access_protect
    BEFORE UPDATE OR DELETE ON order_access
    FOR EACH ROW EXECUTE FUNCTION financial_history_protect();

CREATE FUNCTION financial_history_protect_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'TRUNCATE % запрещён: финансовая история удаляется только DELETE', TG_TABLE_NAME;
END
$$;

CREATE TRIGGER payments_protect_truncate BEFORE TRUNCATE ON payments
    FOR EACH STATEMENT EXECUTE FUNCTION financial_history_protect_truncate();
CREATE TRIGGER money_operations_protect_truncate BEFORE TRUNCATE ON money_operations
    FOR EACH STATEMENT EXECUTE FUNCTION financial_history_protect_truncate();
CREATE TRIGGER refunds_protect_truncate BEFORE TRUNCATE ON refunds
    FOR EACH STATEMENT EXECUTE FUNCTION financial_history_protect_truncate();
CREATE TRIGGER audit_log_protect_truncate BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION financial_history_protect_truncate();
CREATE TRIGGER order_access_protect_truncate BEFORE TRUNCATE ON order_access
    FOR EACH STATEMENT EXECUTE FUNCTION financial_history_protect_truncate();

-- ---------- 8. применение оплаты к заказу ----------
--
-- Тот же алгоритм, что был в payment_service.apply_verified_payment (PR 2), без изменения
-- поведения и формата ответа: блокировка заказа, затем товаров в порядке id, проверка позиций,
-- при нехватке — requires_action без изменения остатков, иначе списание, paid, paid_at и задачи
-- исполнения. SECURITY INVOKER: роль приложения EXECUTE не получает; вызывается владельцем
-- (миграции, тесты, админ) или из SECURITY DEFINER-функции подтверждения платежа.

CREATE FUNCTION apply_paid_order(p_order_id INTEGER) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_status TEXT;
    v_problems JSONB := '[]'::jsonb;
    v_items INTEGER := 0;
    v_tracked INTEGER := 0;
    v_updated INTEGER;
    r RECORD;
BEGIN
    SELECT status INTO v_status FROM orders WHERE id = p_order_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('error', 'order_not_found');
    END IF;
    IF v_status = 'paid' THEN
        RETURN jsonb_build_object('status', 'already_paid');
    END IF;
    IF v_status NOT IN ('pending', 'requires_action') THEN
        RETURN jsonb_build_object('error', 'invalid_order_status');
    END IF;

    FOR r IN
        SELECT p.id, p.quantity, p.is_for_sale, p.inventory_mode, n.needed
        FROM (
            SELECT product_id, sum(quantity)::BIGINT AS needed
            FROM order_items
            WHERE order_id = p_order_id
            GROUP BY product_id
        ) n
        JOIN products p ON p.id = n.product_id
        ORDER BY p.id
        FOR UPDATE OF p
    LOOP
        v_items := v_items + 1;
        IF NOT r.is_for_sale THEN
            v_problems := v_problems || jsonb_build_object('product_id', r.id, 'reason', 'not_for_sale');
        ELSIF r.inventory_mode = 'tracked' AND r.quantity < r.needed THEN
            v_problems := v_problems || jsonb_build_object('product_id', r.id, 'reason', 'insufficient_stock');
        END IF;
        IF r.inventory_mode = 'tracked' THEN
            v_tracked := v_tracked + 1;
        END IF;
    END LOOP;

    IF v_items = 0 THEN
        RETURN jsonb_build_object('error', 'order_has_no_items');
    END IF;

    IF jsonb_array_length(v_problems) > 0 THEN
        UPDATE orders SET status = 'requires_action' WHERE id = p_order_id;
        RETURN jsonb_build_object('error', 'out_of_stock', 'order_id', p_order_id,
                                  'status', 'requires_action', 'problems', v_problems);
    END IF;

    UPDATE products p
    SET quantity = p.quantity - n.needed
    FROM (
        SELECT product_id, sum(quantity)::BIGINT AS needed
        FROM order_items
        WHERE order_id = p_order_id
        GROUP BY product_id
    ) n
    WHERE p.id = n.product_id
      AND p.inventory_mode = 'tracked'
      AND p.quantity >= n.needed;
    GET DIAGNOSTICS v_updated = ROW_COUNT;
    IF v_updated <> v_tracked THEN
        RAISE EXCEPTION 'остатки заказа % изменились во время оплаты', p_order_id;
    END IF;

    UPDATE orders SET status = 'paid', paid_at = now() WHERE id = p_order_id;

    INSERT INTO fulfillments (order_item_id, fulfillment_type)
    SELECT id, fulfillment_type FROM order_items WHERE order_id = p_order_id ORDER BY id
    ON CONFLICT (order_item_id) DO NOTHING;

    RETURN jsonb_build_object('order_id', p_order_id, 'status', 'paid');
END
$$;

-- ---------- 9. функции платёжного процесса (SECURITY DEFINER) ----------
--
-- Кто что вызывает (права выдаёт deploy/db_roles.py):
--   веб-приложение  order_access_issue, order_status_view, payment_attempt_start,
--                   payment_checkout_bind, payment_checkout_mark, provider_event_accept —
--                   всё, что касается заказа, требует SHA-256 секрета этого заказа;
--   обработчик      payment_attempt_bind, payment_attempt_mark, payment_attempt_touch,
--                   payment_record_provider_state;
--   администратор   admin_retry_paid_order, request_refund, admin_resolve_payment.
-- Путь поиска закреплён (раздел 10), EXECUTE у PUBLIC отозван. Доверие: данные о состоянии
-- платежа передаёт обработчик после запроса к провайдеру (ЮKassa не подписывает уведомления);
-- база гарантирует, что оплата учитывается только для существующей попытки, на точную сумму и
-- валюту заказа, один раз, с записью денежной операции и журнала.

-- Выдать секрет доступа: только в транзакции, создающей заказ (состав ещё не закрыт).
CREATE FUNCTION order_access_issue(p_order_id INTEGER, p_token_sha256 BYTEA) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_status TEXT;
    v_sealed BOOLEAN;
BEGIN
    SELECT status, items_sealed INTO v_status, v_sealed FROM orders WHERE id = p_order_id FOR UPDATE;
    IF NOT FOUND OR v_sealed OR v_status <> 'pending' THEN
        RAISE EXCEPTION 'секрет доступа выдаётся только в транзакции создания заказа';
    END IF;
    INSERT INTO order_access (order_id, token_sha256) VALUES (p_order_id, p_token_sha256);
END
$$;

-- Состояние заказа для владельца секрета. NULL — нет заказа или неверный секрет (не различаются).
CREATE FUNCTION order_status_view(p_order_id INTEGER, p_token_sha256 BYTEA) RETURNS JSONB
LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT jsonb_build_object(
        'order', jsonb_build_object('id', o.id, 'status', o.status, 'created_at', o.created_at,
                                    'total_minor', o.total_minor, 'currency', o.currency),
        'items', coalesce((
            SELECT jsonb_agg(jsonb_build_object(
                       'product_id', i.product_id, 'name', i.product_name, 'product_type', i.product_type,
                       'quantity', i.quantity, 'unit_price_minor', i.unit_price_minor,
                       'line_total_minor', i.line_total_minor) ORDER BY i.id)
            FROM order_items i WHERE i.order_id = o.id), '[]'::jsonb),
        'payment', (
            SELECT jsonb_build_object('status', p.status, 'resolution', p.resolution,
                                      'confirmation_url', p.confirmation_url)
            FROM payments p WHERE p.order_id = o.id ORDER BY p.id DESC LIMIT 1),
        'fulfillments', coalesce((
            SELECT jsonb_agg(f.status ORDER BY f.id)
            FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id
            WHERE i.order_id = o.id), '[]'::jsonb),
        'refunds', coalesce((
            SELECT jsonb_agg(r.status ORDER BY r.id) FROM refunds r WHERE r.order_id = o.id), '[]'::jsonb))
    FROM orders o
    JOIN order_access a ON a.order_id = o.id
    WHERE o.id = p_order_id AND a.token_sha256 = p_token_sha256
$$;

-- Начать (или продолжить) оплату: проверка секрета заказа, одна активная попытка на заказ,
-- и ни одной новой попытки, пока есть неразрешённая (requires_action без resolved_at).
CREATE FUNCTION payment_attempt_start(
    p_order_id INTEGER,
    p_token_sha256 BYTEA,
    p_provider TEXT,
    p_new_idempotence_key UUID
) RETURNS TABLE (
    payment_id BIGINT,
    idempotence_key UUID,
    amount_minor BIGINT,
    currency TEXT,
    status TEXT,
    provider_payment_id TEXT,
    confirmation_url TEXT,
    attempt_created_at TIMESTAMPTZ,
    error TEXT
)
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_order RECORD;
    v_payment RECORD;
BEGIN
    SELECT o.id, o.status, o.total_minor, o.currency INTO v_order
    FROM orders o
    JOIN order_access a ON a.order_id = o.id
    WHERE o.id = p_order_id AND a.token_sha256 = p_token_sha256
    FOR UPDATE OF o;
    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::BIGINT, NULL::UUID, NULL::BIGINT, NULL::TEXT, NULL::TEXT,
                            NULL::TEXT, NULL::TEXT, NULL::TIMESTAMPTZ, 'not_found'::TEXT;
        RETURN;
    END IF;
    IF v_order.status <> 'pending' THEN
        RETURN QUERY SELECT NULL::BIGINT, NULL::UUID, NULL::BIGINT, NULL::TEXT, NULL::TEXT,
                            NULL::TEXT, NULL::TEXT, NULL::TIMESTAMPTZ, 'not_payable'::TEXT;
        RETURN;
    END IF;
    IF v_order.total_minor = 0 THEN
        RETURN QUERY SELECT NULL::BIGINT, NULL::UUID, NULL::BIGINT, NULL::TEXT, NULL::TEXT,
                            NULL::TEXT, NULL::TEXT, NULL::TIMESTAMPTZ, 'free_order'::TEXT;
        RETURN;
    END IF;

    SELECT * INTO v_payment FROM payments p
    WHERE p.order_id = p_order_id
      AND p.status IN ('creating', 'unknown', 'pending', 'waiting_for_capture');
    IF NOT FOUND THEN
        IF EXISTS (SELECT 1 FROM payments p
                   WHERE p.order_id = p_order_id AND p.status = 'requires_action' AND p.resolved_at IS NULL) THEN
            -- Предыдущая попытка не разрешена: возможно, по ней ещё придут деньги.
            RETURN QUERY SELECT NULL::BIGINT, NULL::UUID, NULL::BIGINT, NULL::TEXT, NULL::TEXT,
                                NULL::TEXT, NULL::TEXT, NULL::TIMESTAMPTZ, 'payment_under_review'::TEXT;
            RETURN;
        END IF;
        INSERT INTO payments (order_id, provider, idempotence_key, amount_minor, currency)
        VALUES (p_order_id, p_provider, p_new_idempotence_key, v_order.total_minor, v_order.currency)
        RETURNING * INTO v_payment;
        INSERT INTO audit_log (actor, action, order_id, payment_id)
        VALUES ('checkout', 'payment.attempt_created', p_order_id, v_payment.id);
    END IF;

    RETURN QUERY SELECT v_payment.id, v_payment.idempotence_key, v_payment.amount_minor, v_payment.currency,
                        v_payment.status, v_payment.provider_payment_id, v_payment.confirmation_url,
                        v_payment.created_at, NULL::TEXT;
END
$$;

-- Провайдер создал платёж: запомнить его ID и ссылку для покупателя.
-- Результат: статус попытки, или 'id_conflict', если попытка уже привязана к ДРУГОМУ платежу.
CREATE FUNCTION payment_attempt_bind(
    p_payment_id BIGINT,
    p_provider_payment_id TEXT,
    p_confirmation_url TEXT,
    p_provider_test BOOLEAN
) RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_payment RECORD;
BEGIN
    SELECT * INTO v_payment FROM payments WHERE id = p_payment_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'платёжная попытка % не найдена', p_payment_id;
    END IF;
    IF v_payment.provider_payment_id IS NOT NULL THEN
        IF v_payment.provider_payment_id <> p_provider_payment_id THEN
            INSERT INTO audit_log (actor, action, order_id, payment_id)
            VALUES ('payments', 'payment.bind_id_conflict', v_payment.order_id, p_payment_id);
            RETURN 'id_conflict';
        END IF;
        RETURN v_payment.status;
    END IF;
    IF v_payment.status IN ('requires_action', 'failed') THEN
        -- Попытка, завершённая у нас без ID провайдера, нашлась у провайдера: запоминаем ID,
        -- статус не меняется; дальше решает payment_record_provider_state. Ссылку оплаты
        -- сохраняем только для «потерянной» попытки (unresolved_attempt без решения) и только
        -- если обработчик передал её — он передаёт ссылку лишь после полной проверки ответа
        -- провайдера (магазин, режим, сумма, валюта, metadata, статус, https-домен).
        UPDATE payments
        SET provider_payment_id = p_provider_payment_id,
            provider_test = p_provider_test,
            confirmation_url = CASE WHEN v_payment.status = 'requires_action'
                                         AND v_payment.status_reason = 'unresolved_attempt'
                                         AND v_payment.resolved_at IS NULL
                                    THEN p_confirmation_url END
        WHERE id = p_payment_id;
        INSERT INTO audit_log (actor, action, order_id, payment_id)
        VALUES ('payments', 'payment.late_bind', v_payment.order_id, p_payment_id);
        RETURN v_payment.status;
    END IF;
    IF v_payment.status NOT IN ('creating', 'unknown') THEN
        RAISE EXCEPTION 'попытка % в статусе % не ожидает привязки', p_payment_id, v_payment.status;
    END IF;
    UPDATE payments
    SET provider_payment_id = p_provider_payment_id,
        confirmation_url = p_confirmation_url,
        provider_test = p_provider_test,
        status = 'pending'
    WHERE id = p_payment_id;
    INSERT INTO audit_log (actor, action, order_id, payment_id)
    VALUES ('checkout', 'payment.created_at_provider', v_payment.order_id, p_payment_id);
    RETURN 'pending';
END
$$;

-- Результат создания неизвестен (unknown), провайдер отклонил создание (failed) или ответ
-- противоречив / попытка «потеряна» (requires_action — блокирует новые попытки до решения).
CREATE FUNCTION payment_attempt_mark(p_payment_id BIGINT, p_status TEXT, p_reason TEXT) RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_payment RECORD;
BEGIN
    IF p_status NOT IN ('unknown', 'failed', 'requires_action') THEN
        RAISE EXCEPTION 'payment_attempt_mark: недопустимый статус %', p_status;
    END IF;
    SELECT * INTO v_payment FROM payments WHERE id = p_payment_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'платёжная попытка % не найдена', p_payment_id;
    END IF;
    IF v_payment.status NOT IN ('creating', 'unknown') THEN
        RETURN v_payment.status;  -- уже привязана или завершена — ничего не меняем
    END IF;
    IF v_payment.status = p_status THEN
        RETURN p_status;
    END IF;
    UPDATE payments SET status = p_status, status_reason = left(p_reason, 100) WHERE id = p_payment_id;
    INSERT INTO audit_log (actor, action, order_id, payment_id, details)
    VALUES ('payments', 'payment.attempt_' || p_status, v_payment.order_id, p_payment_id,
            jsonb_build_object('reason', left(coalesce(p_reason, ''), 100)));
    RETURN p_status;
END
$$;

-- Варианты для веб-приложения: только для попытки заказа, чей секрет предъявлен.
CREATE FUNCTION payment_checkout_bind(
    p_payment_id BIGINT,
    p_token_sha256 BYTEA,
    p_provider_payment_id TEXT,
    p_confirmation_url TEXT,
    p_provider_test BOOLEAN
) RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM payments p JOIN order_access a ON a.order_id = p.order_id
                   WHERE p.id = p_payment_id AND a.token_sha256 = p_token_sha256) THEN
        RETURN 'not_found';
    END IF;
    RETURN payment_attempt_bind(p_payment_id, p_provider_payment_id, p_confirmation_url, p_provider_test);
END
$$;

CREATE FUNCTION payment_checkout_mark(
    p_payment_id BIGINT,
    p_token_sha256 BYTEA,
    p_status TEXT,
    p_reason TEXT
) RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM payments p JOIN order_access a ON a.order_id = p.order_id
                   WHERE p.id = p_payment_id AND a.token_sha256 = p_token_sha256) THEN
        RETURN 'not_found';
    END IF;
    RETURN payment_attempt_mark(p_payment_id, p_status, p_reason);
END
$$;

-- Обработчик проверил у провайдера неразрешённую попытку и платежа не нашёл: запомнить время.
-- Обработчик проверил у провайдера неразрешённую попытку, но продолжить её нельзя (платёж не
-- найден или найден без пригодной ссылки оплаты): запомнить время проверки и, если передано,
-- причину в журнал. Статус не меняется — попытка остаётся requires_action и блокирует новые.
CREATE FUNCTION payment_attempt_touch(p_payment_id BIGINT, p_note TEXT) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_order INTEGER;
BEGIN
    UPDATE payments SET last_checked_at = now()
    WHERE id = p_payment_id AND status = 'requires_action' AND resolved_at IS NULL
    RETURNING order_id INTO v_order;
    IF FOUND AND p_note IS NOT NULL THEN
        INSERT INTO audit_log (actor, action, order_id, payment_id, details)
        VALUES ('payments', 'payment.unresolved_checked', v_order, p_payment_id,
                jsonb_build_object('note', left(p_note, 100)));
    END IF;
END
$$;

-- Принять уведомление в очередь. Повторная доставка во время обработки не трогает аренду.
CREATE FUNCTION provider_event_accept(
    p_provider TEXT,
    p_event_type TEXT,
    p_object_id TEXT,
    p_initial_status TEXT
) RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_id BIGINT;
BEGIN
    IF p_initial_status NOT IN ('received', 'ignored') THEN
        RAISE EXCEPTION 'provider_event_accept: недопустимый статус %', p_initial_status;
    END IF;
    INSERT INTO provider_events AS e (provider, event_type, object_id, status)
    VALUES (p_provider, p_event_type, p_object_id, p_initial_status)
    ON CONFLICT (provider, event_type, object_id) DO UPDATE
    SET deliveries = LEAST(e.deliveries, 1000000000) + 1,
        last_received_at = now(),
        recheck_requested = e.recheck_requested OR e.status = 'processing',
        next_attempt_at = CASE WHEN e.status = 'received' THEN LEAST(e.next_attempt_at, now())
                               ELSE e.next_attempt_at END
    RETURNING e.id INTO v_id;
    RETURN v_id;
END
$$;

-- Применить ПРОВЕРЕННОЕ у провайдера состояние платежа (ответ GET /payments/{id} или поиска).
-- Повторные и устаревшие состояния безопасны: конечный статус не меняется, оплата учитывается
-- один раз (блокировка строки платежа и заказа, уникальные индексы).
CREATE FUNCTION payment_record_provider_state(
    p_payment_id BIGINT,
    p_provider_payment_id TEXT,
    p_status TEXT,
    p_amount_minor BIGINT,
    p_currency TEXT,
    p_provider_test BOOLEAN,
    p_expected_test BOOLEAN,
    p_reason TEXT,
    -- Ссылка оплаты из ПРОВЕРЕННОГО ответа провайдера (обработчик передаёт её только после
    -- проверки магазина, режима, суммы, валюты, metadata, статуса и https-домена). Нужна,
    -- чтобы «потерянная» попытка, найденная в pending, возвращалась к оплате вместе со ссылкой
    -- в той же транзакции и под той же блокировкой строки.
    p_confirmation_url TEXT DEFAULT NULL
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_payment RECORD;
    v_reopen BOOLEAN;
    v_new TEXT;
    v_mismatch TEXT;
    v_apply JSONB;
BEGIN
    IF p_status NOT IN ('pending', 'waiting_for_capture', 'succeeded', 'canceled', 'requires_action') THEN
        RAISE EXCEPTION 'payment_record_provider_state: недопустимый статус %', p_status;
    END IF;

    SELECT * INTO v_payment FROM payments WHERE id = p_payment_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'платёжная попытка % не найдена', p_payment_id;
    END IF;
    IF v_payment.provider_payment_id IS NULL OR v_payment.provider_payment_id <> p_provider_payment_id THEN
        RAISE EXCEPTION 'ID платежа у провайдера не совпадает с попыткой %', p_payment_id;
    END IF;

    -- Порядок блокировок во всех функциях: платёж, затем заказ, затем товары.
    PERFORM 1 FROM orders WHERE id = v_payment.order_id FOR UPDATE;

    UPDATE payments SET last_checked_at = now() WHERE id = p_payment_id;

    -- «Потерянная» попытка, которую ещё никто не разрешал, продолжает обычную обработку.
    v_reopen := v_payment.status = 'requires_action' AND v_payment.status_reason = 'unresolved_attempt'
                AND v_payment.resolved_at IS NULL
                AND NOT EXISTS (SELECT 1 FROM money_operations WHERE payment_id = p_payment_id);

    -- Конечные статусы не меняются: повторные и опоздавшие события ничего не делают.
    -- Исключение — деньги: если провайдер сообщает succeeded по попытке, которая у нас уже
    -- завершена иначе, факт оплаты всё равно учитывается, создаётся запрос на возврат, а
    -- прежнее решение администратора снимается — случай снова требует внимания.
    IF v_payment.status IN ('succeeded', 'canceled', 'failed', 'requires_action') AND NOT v_reopen THEN
        IF p_status = 'succeeded' AND p_amount_minor > 0 AND p_currency IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM money_operations WHERE payment_id = p_payment_id AND kind = 'payment') THEN
            INSERT INTO money_operations (kind, payment_id, order_id, provider, provider_operation_id,
                                          amount_minor, currency, provider_test)
            VALUES ('payment', p_payment_id, v_payment.order_id, v_payment.provider, p_provider_payment_id,
                    p_amount_minor, p_currency, coalesce(p_provider_test, FALSE));
            INSERT INTO refunds (payment_id, order_id, amount_minor, currency, reason)
            VALUES (p_payment_id, v_payment.order_id, p_amount_minor, p_currency, 'payment_after_final_state');
            -- Прежнее решение снимается, причина меняется: попытка снова ждёт человека
            -- (возврат), но больше не считается «потерянной» и не обрабатывается повторно.
            UPDATE payments SET resolution = NULL, resolved_at = NULL, status_reason = 'late_payment'
            WHERE id = p_payment_id;
            INSERT INTO audit_log (actor, action, order_id, payment_id, details)
            VALUES ('payments', 'payment.late_success', v_payment.order_id, p_payment_id,
                    jsonb_build_object('local_status', v_payment.status, 'amount_minor', p_amount_minor,
                                       'currency', p_currency, 'previous_resolution', v_payment.resolution));
            RETURN jsonb_build_object('result', 'late_success_recorded', 'payment_status', v_payment.status);
        END IF;
        RETURN jsonb_build_object('result', 'already_final', 'payment_status', v_payment.status);
    END IF;

    IF v_reopen THEN
        INSERT INTO audit_log (actor, action, order_id, payment_id, details)
        VALUES ('payments', 'payment.unresolved_found', v_payment.order_id, p_payment_id,
                jsonb_build_object('provider_status', p_status));
    END IF;

    IF p_status IN ('pending', 'waiting_for_capture') THEN
        v_new := CASE WHEN p_status = 'waiting_for_capture' OR v_payment.status = 'waiting_for_capture'
                      THEN 'waiting_for_capture' ELSE 'pending' END;
        IF v_reopen AND v_new = 'pending' THEN
            -- Вернуть «потерянную» попытку к оплате можно только со ссылкой: она сохраняется
            -- одним UPDATE со сменой статуса. Без пригодной ссылки попытка остаётся
            -- requires_action (новые попытки по-прежнему заблокированы).
            IF p_confirmation_url IS NULL OR p_confirmation_url !~ '^https://[^/@[:space:]]+/'
               OR char_length(p_confirmation_url) > 2048 THEN
                RETURN jsonb_build_object('result', 'requires_action', 'payment_status', 'requires_action',
                                          'reason', 'no_valid_confirmation_url');
            END IF;
            UPDATE payments SET status = 'pending', status_reason = NULL, confirmation_url = p_confirmation_url
            WHERE id = p_payment_id;
            INSERT INTO audit_log (actor, action, order_id, payment_id)
            VALUES ('payments', 'payment.unresolved_resumed', v_payment.order_id, p_payment_id);
            RETURN jsonb_build_object('result', 'not_final', 'payment_status', 'pending');
        END IF;
        IF v_new <> v_payment.status THEN
            UPDATE payments SET status = v_new, status_reason = NULL WHERE id = p_payment_id;
        END IF;
        RETURN jsonb_build_object('result', 'not_final', 'payment_status', v_new);
    END IF;

    IF p_status = 'canceled' THEN
        UPDATE payments SET status = 'canceled', status_reason = left(p_reason, 100) WHERE id = p_payment_id;
        INSERT INTO audit_log (actor, action, order_id, payment_id, details)
        VALUES ('payments', 'payment.canceled', v_payment.order_id, p_payment_id,
                jsonb_build_object('reason', left(coalesce(p_reason, ''), 100)));
        RETURN jsonb_build_object('result', 'canceled', 'payment_status', 'canceled');
    END IF;

    IF p_status = 'requires_action' THEN
        UPDATE payments SET status = 'requires_action', status_reason = left(p_reason, 100) WHERE id = p_payment_id;
        INSERT INTO audit_log (actor, action, order_id, payment_id, details)
        VALUES ('payments', 'payment.requires_action', v_payment.order_id, p_payment_id,
                jsonb_build_object('reason', left(coalesce(p_reason, ''), 100)));
        RETURN jsonb_build_object('result', 'requires_action', 'payment_status', 'requires_action',
                                  'reason', left(coalesce(p_reason, ''), 100));
    END IF;

    -- succeeded: деньги получены. Сначала сверка, затем учёт.
    IF p_amount_minor IS NULL OR p_amount_minor <= 0 OR p_currency IS NULL THEN
        RAISE EXCEPTION 'для succeeded нужны сумма и валюта от провайдера';
    END IF;
    IF p_amount_minor <> v_payment.amount_minor THEN
        v_mismatch := 'amount_mismatch';
    ELSIF p_currency <> v_payment.currency THEN
        v_mismatch := 'currency_mismatch';
    ELSIF p_provider_test IS DISTINCT FROM p_expected_test THEN
        v_mismatch := 'test_mode_mismatch';
    ELSIF EXISTS (SELECT 1 FROM payments WHERE order_id = v_payment.order_id AND status = 'succeeded') THEN
        v_mismatch := 'duplicate_payment';
    END IF;

    -- Факт получения денег учитывается всегда — сумма и валюта как у провайдера.
    INSERT INTO money_operations (kind, payment_id, order_id, provider, provider_operation_id,
                                  amount_minor, currency, provider_test)
    VALUES ('payment', p_payment_id, v_payment.order_id, v_payment.provider, p_provider_payment_id,
            p_amount_minor, p_currency, coalesce(p_provider_test, FALSE));

    IF v_mismatch IS NOT NULL THEN
        UPDATE payments SET status = 'requires_action', status_reason = v_mismatch WHERE id = p_payment_id;
        INSERT INTO audit_log (actor, action, order_id, payment_id, details)
        VALUES ('payments', 'payment.received_requires_action', v_payment.order_id, p_payment_id,
                jsonb_build_object('reason', v_mismatch, 'amount_minor', p_amount_minor, 'currency', p_currency));
        IF v_mismatch <> 'test_mode_mismatch' THEN
            -- Деньги получены, но к заказу их отнести нельзя: возврат ждёт решения человека.
            INSERT INTO refunds (payment_id, order_id, amount_minor, currency, reason)
            VALUES (p_payment_id, v_payment.order_id, p_amount_minor, p_currency, v_mismatch);
        END IF;
        RETURN jsonb_build_object('result', 'requires_action', 'payment_status', 'requires_action',
                                  'reason', v_mismatch);
    END IF;

    UPDATE payments SET status = 'succeeded', status_reason = NULL WHERE id = p_payment_id;
    v_apply := apply_paid_order(v_payment.order_id);
    INSERT INTO audit_log (actor, action, order_id, payment_id, details)
    VALUES ('payments', 'payment.succeeded', v_payment.order_id, p_payment_id,
            jsonb_build_object('order_result', coalesce(v_apply->>'status', v_apply->>'error')));

    IF v_apply ? 'error' AND v_apply->>'error' <> 'out_of_stock' THEN
        -- Деньги получены, но заказ нельзя провести (например, отменён) — возврат для человека.
        INSERT INTO refunds (payment_id, order_id, amount_minor, currency, reason)
        VALUES (p_payment_id, v_payment.order_id, p_amount_minor, p_currency, 'order_not_payable');
    END IF;

    RETURN jsonb_build_object('result', 'succeeded', 'payment_status', 'succeeded', 'order', v_apply);
END
$$;

-- Администратор разрешил спорную попытку (requires_action). После этого заказ снова можно
-- оплатить. not_paid_confirmed — только если по попытке не учтено денег.
CREATE FUNCTION admin_resolve_payment(p_payment_id BIGINT, p_resolution TEXT, p_note TEXT) RETURNS VOID
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_payment RECORD;
BEGIN
    IF p_resolution NOT IN ('not_paid_confirmed', 'handled_manually') THEN
        RAISE EXCEPTION 'решение: not_paid_confirmed или handled_manually';
    END IF;
    IF p_note IS NULL OR char_length(btrim(p_note)) = 0 THEN
        RAISE EXCEPTION 'укажите, на основании чего принято решение';
    END IF;
    SELECT * INTO v_payment FROM payments WHERE id = p_payment_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'платёж % не найден', p_payment_id;
    END IF;
    IF v_payment.status <> 'requires_action' OR v_payment.resolved_at IS NOT NULL THEN
        RAISE EXCEPTION 'платёж % не ожидает решения (статус %)', p_payment_id, v_payment.status;
    END IF;
    IF p_resolution = 'not_paid_confirmed'
       AND EXISTS (SELECT 1 FROM money_operations WHERE payment_id = p_payment_id) THEN
        RAISE EXCEPTION 'по платежу % учтены деньги — not_paid_confirmed невозможно', p_payment_id;
    END IF;
    UPDATE payments SET resolution = p_resolution, resolved_at = now() WHERE id = p_payment_id;
    INSERT INTO audit_log (actor, action, order_id, payment_id, details)
    VALUES ('admin', 'payment.resolved', v_payment.order_id, p_payment_id,
            jsonb_build_object('resolution', p_resolution, 'reason', v_payment.status_reason,
                               'note', left(p_note, 300)));
END
$$;

-- Административные функции (роль администратора, не приложение).

-- Повторно провести оплаченный заказ из requires_action (например, после пополнения склада).
CREATE FUNCTION admin_retry_paid_order(p_order_id INTEGER) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_result JSONB;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM payments p JOIN money_operations m ON m.payment_id = p.id AND m.kind = 'payment'
        WHERE p.order_id = p_order_id AND p.status = 'succeeded'
    ) THEN
        RAISE EXCEPTION 'у заказа % нет учтённой успешной оплаты', p_order_id;
    END IF;
    v_result := apply_paid_order(p_order_id);
    INSERT INTO audit_log (actor, action, order_id, details)
    VALUES ('admin', 'order.retry_paid', p_order_id,
            jsonb_build_object('result', coalesce(v_result->>'status', v_result->>'error')));
    RETURN v_result;
END
$$;

-- Запросить возврат по учтённой оплате (исполнение у провайдера — следующий этап).
CREATE FUNCTION request_refund(p_payment_id BIGINT, p_amount_minor BIGINT, p_reason TEXT) RETURNS BIGINT
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_payment RECORD;
    v_received RECORD;
    v_requested BIGINT;
    v_id BIGINT;
BEGIN
    SELECT * INTO v_payment FROM payments WHERE id = p_payment_id FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'платёж % не найден', p_payment_id;
    END IF;
    -- Сумма и валюта — по ФАКТИЧЕСКИ полученным деньгам (money_operations), а не по заказу:
    -- при currency_mismatch деньги пришли не в валюте заказа.
    SELECT amount_minor, currency INTO v_received
    FROM money_operations WHERE payment_id = p_payment_id AND kind = 'payment';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'по платежу % деньги не учтены — возвращать нечего', p_payment_id;
    END IF;
    SELECT coalesce(sum(amount_minor), 0) INTO v_requested
    FROM refunds WHERE payment_id = p_payment_id AND status <> 'canceled';
    IF p_amount_minor IS NULL OR p_amount_minor <= 0 OR v_requested + p_amount_minor > v_received.amount_minor THEN
        RAISE EXCEPTION 'сумма возврата больше доступной (получено % %, уже запрошено %)',
            v_received.amount_minor, v_received.currency, v_requested;
    END IF;
    INSERT INTO refunds (payment_id, order_id, amount_minor, currency, reason)
    VALUES (p_payment_id, v_payment.order_id, p_amount_minor, v_received.currency, left(p_reason, 200))
    RETURNING id INTO v_id;
    INSERT INTO audit_log (actor, action, order_id, payment_id, details)
    VALUES ('admin', 'refund.requested', v_payment.order_id, p_payment_id,
            jsonb_build_object('refund_id', v_id, 'amount_minor', p_amount_minor,
                               'currency', v_received.currency));
    RETURN v_id;
END
$$;

-- ---------- 10. доверенный search_path и права на функции ----------
--
-- Как в 0002: путь pg_catalog, <схема магазина>, pg_temp — временные объекты не подменят
-- таблицы (обязательно для SECURITY DEFINER). EXECUTE у PUBLIC отозван у всех функций,
-- которые вызываются напрямую: права выдаёт deploy/db_roles.py.

DO $$
DECLARE
    shop_schema TEXT := current_schema();
    fn REGPROCEDURE;
BEGIN
    IF shop_schema IS NULL OR shop_schema LIKE 'pg\_temp%' OR shop_schema = 'pg_catalog' THEN
        RAISE EXCEPTION 'миграция должна выполняться со схемой магазина в search_path (сейчас: %)', shop_schema;
    END IF;
    FOR fn IN
        SELECT p.oid::REGPROCEDURE
        FROM pg_proc p
        WHERE p.pronamespace = shop_schema::REGNAMESPACE
          AND p.proname IN (
              'payments_guard', 'refunds_guard', 'financial_history_protect', 'financial_history_protect_truncate',
              'apply_paid_order', 'order_access_issue', 'order_status_view', 'payment_attempt_start',
              'payment_attempt_bind', 'payment_attempt_mark', 'payment_checkout_bind', 'payment_checkout_mark',
              'payment_attempt_touch', 'provider_event_accept', 'payment_record_provider_state',
              'admin_resolve_payment', 'admin_retry_paid_order', 'request_refund')
    LOOP
        EXECUTE format('ALTER FUNCTION %s SET search_path = pg_catalog, %I, pg_temp', fn, shop_schema);
    END LOOP;
    FOR fn IN
        SELECT p.oid::REGPROCEDURE
        FROM pg_proc p
        WHERE p.pronamespace = shop_schema::REGNAMESPACE
          AND p.proname IN (
              'apply_paid_order', 'order_access_issue', 'order_status_view', 'payment_attempt_start',
              'payment_attempt_bind', 'payment_attempt_mark', 'payment_checkout_bind', 'payment_checkout_mark',
              'payment_attempt_touch', 'provider_event_accept', 'payment_record_provider_state',
              'admin_resolve_payment', 'admin_retry_paid_order', 'request_refund')
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', fn);
    END LOOP;
END
$$;

-- ================= выдача цифровых ключей (миграция 0004) =================

-- 0004 — автоматическая выдача цифровых ключей (PR5).
--
-- Что делает:
--   1. shop_security_settings  — какая группа PostgreSQL считается администраторами магазина
--                                (заполняет deploy/db_roles.py при настройке ролей; миграция — нет).
--   2. encryption_key_registry — ID и проверочные значения ключей шифрования и отпечатков
--                                (НЕ сами ключи: они только в окружении процессов).
--   3. digital_key_batches     — партии импорта.
--   4. digital_keys            — ключи: шифротекст AES-256-GCM, nonce, HMAC-отпечаток, назначение.
--   5. fulfillments            — три новых столбца (status_note, refund_hold_released_at,
--                                cancel_reason). Существующие строки не меняются.
--   6. Представления: product_sellable_stock (продаваемый остаток S), digital_key_stock,
--      fulfillment_issues.
--   7. Триггеры целостности: ключи, задачи выдачи, остаток товара.
--   8. apply_paid_order — переопределена: для позиций digital_keys оплата проходит только при
--      достаточном запасе свободных ключей (R1). Формат ответа прежний.
--   9. Функции: назначение ключей (worker), выдача покупателю (app), административные
--      (только члены группы администраторов, проверка в базе).
--
-- Миграции 0000–0003 не меняются. Данные существующих таблиц не изменяются.
-- Инвариант для товара с fulfillment_type = 'digital_keys':  A ≥ W + Q, где
--   A — свободные ключи, W — оплаченные невыданные единицы, Q — products.quantity.
-- Продаваемый остаток S = max(0, min(Q, A − W)). Подробно — DIGITAL_KEYS.md.
--
-- Порядок блокировок во всех путях (DIGITAL_KEYS.md, «Блокировки»):
--   fulfillments → payments → orders → products (по id) → advisory(импорт) → advisory(товар) → digital_keys (по id)

-- ---------- 1. настройки безопасности ----------

CREATE TABLE shop_security_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE,
    admin_group TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT shop_security_settings_singleton_check CHECK (singleton),
    CONSTRAINT shop_security_settings_admin_group_check CHECK (admin_group ~ '^[a-z_][a-z0-9_]{0,62}$')
);

-- ---------- 2. реестр ключей шифрования (без секретов) ----------

CREATE TABLE encryption_key_registry (
    purpose TEXT NOT NULL,
    key_id TEXT NOT NULL,
    check_value BYTEA NOT NULL,
    registered_by TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    retired_at TIMESTAMPTZ,
    PRIMARY KEY (purpose, key_id),
    CONSTRAINT encryption_key_registry_purpose_check CHECK (purpose IN ('encryption', 'fingerprint')),
    CONSTRAINT encryption_key_registry_key_id_check CHECK (key_id ~ '^[a-z0-9_-]{1,32}$'),
    CONSTRAINT encryption_key_registry_check_value_check CHECK (octet_length(check_value) = 16)
);

-- ---------- 3. партии импорта ----------

CREATE TABLE digital_key_batches (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    source_label TEXT NOT NULL,
    imported_by TEXT NOT NULL,
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted INTEGER NOT NULL,
    inserted INTEGER NOT NULL,
    duplicates_in_batch INTEGER NOT NULL,
    duplicates_existing INTEGER NOT NULL,
    quantity_added INTEGER NOT NULL,
    CONSTRAINT digital_key_batches_source_label_check CHECK (char_length(source_label) BETWEEN 1 AND 100),
    CONSTRAINT digital_key_batches_counts_check CHECK (
        submitted BETWEEN 1 AND 10000 AND inserted >= 1 AND duplicates_in_batch >= 0
        AND duplicates_existing >= 0 AND quantity_added BETWEEN 0 AND inserted
        AND inserted + duplicates_in_batch + duplicates_existing = submitted)
);

CREATE INDEX digital_key_batches_product_idx ON digital_key_batches (product_id);

-- ---------- 4. ключи ----------

CREATE TABLE digital_keys (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    public_id UUID NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id),
    batch_id BIGINT NOT NULL REFERENCES digital_key_batches(id),
    enc_key_id TEXT NOT NULL,
    nonce BYTEA NOT NULL,
    ciphertext BYTEA NOT NULL,
    fingerprint BYTEA NOT NULL,
    fingerprint_key_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'available',
    fulfillment_id BIGINT REFERENCES fulfillments(id),
    order_item_id BIGINT REFERENCES order_items(id),
    imported_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    assigned_at TIMESTAMPTZ,
    withdrawn_at TIMESTAMPTZ,
    withdraw_reason TEXT,
    CONSTRAINT digital_keys_public_id_key UNIQUE (public_id),
    CONSTRAINT digital_keys_fingerprint_key UNIQUE (fingerprint),
    CONSTRAINT digital_keys_enc_key_id_check CHECK (enc_key_id ~ '^[a-z0-9_-]{1,32}$'),
    CONSTRAINT digital_keys_fingerprint_key_id_check CHECK (fingerprint_key_id ~ '^[a-z0-9_-]{1,32}$'),
    CONSTRAINT digital_keys_nonce_check CHECK (octet_length(nonce) = 12),
    CONSTRAINT digital_keys_ciphertext_check CHECK (octet_length(ciphertext) BETWEEN 17 AND 4112),
    CONSTRAINT digital_keys_fingerprint_check CHECK (octet_length(fingerprint) = 32),
    CONSTRAINT digital_keys_withdraw_reason_check CHECK (char_length(withdraw_reason) <= 200),
    CONSTRAINT digital_keys_state_check CHECK (
        (status = 'available' AND fulfillment_id IS NULL AND order_item_id IS NULL
            AND assigned_at IS NULL AND withdrawn_at IS NULL)
        OR (status = 'assigned' AND fulfillment_id IS NOT NULL AND order_item_id IS NOT NULL
            AND assigned_at IS NOT NULL AND withdrawn_at IS NULL)
        OR (status = 'withdrawn' AND fulfillment_id IS NULL AND order_item_id IS NULL
            AND withdrawn_at IS NOT NULL))
);

CREATE INDEX digital_keys_available_idx ON digital_keys (product_id, id) WHERE status = 'available';
CREATE INDEX digital_keys_fulfillment_idx ON digital_keys (fulfillment_id) WHERE fulfillment_id IS NOT NULL;
CREATE INDEX digital_keys_batch_idx ON digital_keys (batch_id);

-- ---------- 5. задачи выдачи: новые столбцы ----------

ALTER TABLE fulfillments
    ADD COLUMN status_note TEXT,
    ADD COLUMN refund_hold_released_at TIMESTAMPTZ,
    ADD COLUMN cancel_reason TEXT,
    ADD CONSTRAINT fulfillments_status_note_check CHECK (char_length(status_note) <= 300),
    ADD CONSTRAINT fulfillments_cancel_reason_check CHECK (char_length(cancel_reason) <= 200);

CREATE INDEX fulfillments_digital_open_idx ON fulfillments (order_item_id)
    WHERE fulfillment_type = 'digital_keys' AND status IN ('pending', 'in_progress', 'requires_action', 'failed');

-- ---------- 6. учёт запаса ----------

-- A и W для товара. Читает без блокировок: вызывающий держит нужную блокировку сам
-- (строку products — при проверке инварианта).
CREATE FUNCTION digital_key_stock_counts(p_product_id INTEGER, OUT available BIGINT, OUT awaiting BIGINT)
LANGUAGE sql STABLE AS $$
    SELECT
        (SELECT count(*) FROM digital_keys k WHERE k.product_id = p_product_id AND k.status = 'available'),
        (SELECT coalesce(sum(i.quantity), 0)
         FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id
         WHERE i.product_id = p_product_id AND f.fulfillment_type = 'digital_keys'
           AND f.status IN ('pending', 'in_progress', 'requires_action', 'failed'))
$$;

-- Представления не вызывают функций: право EXECUTE проверялось бы у читающей роли, а таблицы
-- ключей и задач ей недоступны. Подзапросы читаются с правами владельца представления.

-- Продаваемый остаток S. Для товаров не digital_keys — просто quantity.
CREATE VIEW product_sellable_stock AS
SELECT p.id AS product_id,
       CASE WHEN p.fulfillment_type = 'digital_keys'
            THEN GREATEST(0, LEAST(p.quantity::BIGINT, a.available - w.awaiting))::INTEGER
            ELSE p.quantity END AS sellable_quantity
FROM products p
CROSS JOIN LATERAL (SELECT count(*) AS available FROM digital_keys k
                    WHERE k.product_id = p.id AND k.status = 'available') a
CROSS JOIN LATERAL (SELECT coalesce(sum(i.quantity), 0) AS awaiting
                    FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id
                    WHERE i.product_id = p.id AND f.fulfillment_type = 'digital_keys'
                      AND f.status IN ('pending', 'in_progress', 'requires_action', 'failed')) w;

CREATE VIEW digital_key_stock AS
SELECT p.id AS product_id, p.name, p.fulfillment_type, p.quantity,
       a.available, a.assigned, a.withdrawn, w.awaiting,
       a.available - w.awaiting - p.quantity AS surplus
FROM products p
CROSS JOIN LATERAL (SELECT count(*) FILTER (WHERE k.status = 'available') AS available,
                           count(*) FILTER (WHERE k.status = 'assigned') AS assigned,
                           count(*) FILTER (WHERE k.status = 'withdrawn') AS withdrawn
                    FROM digital_keys k WHERE k.product_id = p.id) a
CROSS JOIN LATERAL (SELECT coalesce(sum(i.quantity), 0) AS awaiting
                    FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id
                    WHERE i.product_id = p.id AND f.fulfillment_type = 'digital_keys'
                      AND f.status IN ('pending', 'in_progress', 'requires_action', 'failed')) w
WHERE p.product_type = 'digital_key';

CREATE VIEW fulfillment_issues AS
SELECT f.id AS fulfillment_id, i.order_id, i.id AS order_item_id, i.product_id, i.product_name,
       f.fulfillment_type, i.quantity, f.status, f.attempts, f.status_note, f.last_error,
       f.refund_hold_released_at, f.updated_at
FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id
WHERE f.status IN ('requires_action', 'failed');

-- ---------- 7. триггеры целостности ----------

-- Назначено ли задаче ровно quantity ключей. SECURITY DEFINER: триггер задач выполняется с правами
-- обновляющей роли (worker), а таблица ключей ей недоступна. EXECUTE — только worker (db_roles.py).
CREATE FUNCTION digital_keys_task_complete(p_fulfillment_id BIGINT) RETURNS BOOLEAN
LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT (SELECT count(*) FROM digital_keys k WHERE k.fulfillment_id = f.id AND k.status = 'assigned')
           = i.quantity
    FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id
    WHERE f.id = p_fulfillment_id
$$;

CREATE FUNCTION digital_keys_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_type TEXT;
    v_task RECORD;
    v_assigned BIGINT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF current_setting('digital_store.allow_history_delete', true) = 'on' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'ключи не удаляются (изъятие — admin_withdraw_digital_key)';
    END IF;

    IF TG_OP = 'INSERT' THEN
        SELECT product_type INTO v_type FROM products WHERE id = NEW.product_id;
        IF v_type IS DISTINCT FROM 'digital_key' THEN
            RAISE EXCEPTION 'ключи загружаются только для товара типа digital_key';
        END IF;
        IF NEW.status <> 'available' THEN
            RAISE EXCEPTION 'новый ключ создаётся только свободным';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM digital_key_batches b WHERE b.id = NEW.batch_id AND b.product_id = NEW.product_id) THEN
            RAISE EXCEPTION 'партия ключа относится к другому товару';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.public_id <> OLD.public_id OR NEW.product_id <> OLD.product_id OR NEW.batch_id <> OLD.batch_id
       OR NEW.enc_key_id <> OLD.enc_key_id OR NEW.nonce <> OLD.nonce OR NEW.ciphertext <> OLD.ciphertext
       OR NEW.fingerprint <> OLD.fingerprint OR NEW.fingerprint_key_id <> OLD.fingerprint_key_id
       OR NEW.imported_at <> OLD.imported_at THEN
        RAISE EXCEPTION 'содержимое ключа % неизменяемо', OLD.id;
    END IF;
    IF NEW.status = OLD.status THEN
        IF NEW.fulfillment_id IS DISTINCT FROM OLD.fulfillment_id OR NEW.order_item_id IS DISTINCT FROM OLD.order_item_id
           OR NEW.assigned_at IS DISTINCT FROM OLD.assigned_at OR NEW.withdrawn_at IS DISTINCT FROM OLD.withdrawn_at
           OR NEW.withdraw_reason IS DISTINCT FROM OLD.withdraw_reason THEN
            RAISE EXCEPTION 'назначение и изъятие ключа % не меняются', OLD.id;
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status <> 'available' THEN
        RAISE EXCEPTION 'ключ % уже %: состояние конечное', OLD.id, OLD.status;
    END IF;
    IF NEW.status = 'assigned' THEN
        SELECT f.status, f.fulfillment_type, f.order_item_id, i.product_id, i.quantity, o.status AS order_status
        INTO v_task
        FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id JOIN orders o ON o.id = i.order_id
        WHERE f.id = NEW.fulfillment_id;
        IF NOT FOUND OR v_task.fulfillment_type <> 'digital_keys' OR v_task.status <> 'pending'
           OR v_task.order_status <> 'paid' OR v_task.order_item_id <> NEW.order_item_id
           OR v_task.product_id <> NEW.product_id THEN
            RAISE EXCEPTION 'ключ % нельзя назначить: задача не ожидает выдачи этого товара по оплаченному заказу', OLD.id;
        END IF;
        -- Строки, обработанные этой же командой раньше, уже видны (BEFORE ROW).
        SELECT count(*) INTO v_assigned FROM digital_keys
        WHERE fulfillment_id = NEW.fulfillment_id AND status = 'assigned' AND id <> OLD.id;
        IF v_assigned + 1 > v_task.quantity THEN
            RAISE EXCEPTION 'задаче % нельзя назначить больше % ключей', NEW.fulfillment_id, v_task.quantity;
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER digital_keys_guard
    BEFORE INSERT OR UPDATE OR DELETE ON digital_keys
    FOR EACH ROW EXECUTE FUNCTION digital_keys_guard();

CREATE FUNCTION digital_keys_protect_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'TRUNCATE % запрещён', TG_TABLE_NAME;
END
$$;

CREATE TRIGGER digital_keys_protect_truncate BEFORE TRUNCATE ON digital_keys
    FOR EACH STATEMENT EXECUTE FUNCTION digital_keys_protect_truncate();
CREATE TRIGGER digital_key_batches_protect_truncate BEFORE TRUNCATE ON digital_key_batches
    FOR EACH STATEMENT EXECUTE FUNCTION digital_keys_protect_truncate();
CREATE TRIGGER encryption_key_registry_protect_truncate BEFORE TRUNCATE ON encryption_key_registry
    FOR EACH STATEMENT EXECUTE FUNCTION digital_keys_protect_truncate();

-- Партии и реестр: только добавление (в реестре можно один раз поставить retired_at).
CREATE FUNCTION digital_key_records_protect() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('digital_store.allow_history_delete', true) = 'on' THEN
        RETURN OLD;
    END IF;
    -- Вложенные IF: поля NEW.purpose и др. есть только у реестра (у партий выражение не компилируется).
    IF TG_OP = 'UPDATE' AND TG_TABLE_NAME = 'encryption_key_registry' THEN
        IF NEW.purpose = OLD.purpose AND NEW.key_id = OLD.key_id AND NEW.check_value = OLD.check_value
           AND NEW.registered_by = OLD.registered_by AND NEW.registered_at = OLD.registered_at
           AND OLD.retired_at IS NULL THEN
            RETURN NEW;
        END IF;
    END IF;
    RAISE EXCEPTION '% запрещён для %', TG_OP, TG_TABLE_NAME;
END
$$;

CREATE TRIGGER digital_key_batches_protect BEFORE UPDATE OR DELETE ON digital_key_batches
    FOR EACH ROW EXECUTE FUNCTION digital_key_records_protect();
CREATE TRIGGER encryption_key_registry_protect BEFORE UPDATE OR DELETE ON encryption_key_registry
    FOR EACH ROW EXECUTE FUNCTION digital_key_records_protect();

-- Задачи выдачи.
CREATE FUNCTION fulfillments_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    -- Владелец таблицы = SECURITY DEFINER-функции магазина (или сам мигратор). Проверка без
    -- вызова функций: триггер выполняется с правами обновляющей роли.
    v_owner BOOLEAN := (SELECT c.relowner FROM pg_class c WHERE c.oid = TG_RELID)
                       = (SELECT r.oid FROM pg_roles r WHERE r.rolname = current_user);
BEGIN
    IF NEW.order_item_id <> OLD.order_item_id OR NEW.fulfillment_type <> OLD.fulfillment_type
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'позиция и тип задачи выдачи % неизменяемы', OLD.id;
    END IF;
    IF NOT v_owner AND (NEW.refund_hold_released_at IS DISTINCT FROM OLD.refund_hold_released_at
                        OR NEW.cancel_reason IS DISTINCT FROM OLD.cancel_reason) THEN
        RAISE EXCEPTION 'снятие остановки и отмену задачи % выполняет только администратор', OLD.id;
    END IF;
    IF NEW.status IS DISTINCT FROM OLD.status THEN
        IF OLD.fulfillment_type = 'digital_keys' AND OLD.status IN ('fulfilled', 'cancelled') THEN
            RAISE EXCEPTION 'задача выдачи ключей % уже %: состояние конечное', OLD.id, OLD.status;
        END IF;
        IF NOT v_owner AND (OLD.status IN ('requires_action', 'failed', 'cancelled') OR NEW.status = 'cancelled') THEN
            RAISE EXCEPTION 'возобновление и отмену задачи % выполняет только администратор', OLD.id;
        END IF;
        IF NEW.status = 'fulfilled' AND NEW.fulfillment_type = 'digital_keys'
           AND NOT digital_keys_task_complete(NEW.id) THEN
            RAISE EXCEPTION 'задача % не может быть выполнена: назначены не все ключи', OLD.id;
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER fulfillments_guard
    BEFORE UPDATE ON fulfillments
    FOR EACH ROW EXECUTE FUNCTION fulfillments_guard();

-- Остаток товара с автоматической выдачей не может превысить обеспеченный ключами (A − W).
-- Уменьшение остатка (в том числе при оплате в apply_paid_order) не проверяется.
CREATE FUNCTION products_digital_stock_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_counts RECORD;
BEGIN
    IF NEW.fulfillment_type = 'digital_keys'
       AND (NEW.quantity > OLD.quantity OR OLD.fulfillment_type IS DISTINCT FROM 'digital_keys') THEN
        SELECT * INTO v_counts FROM digital_key_stock_counts(NEW.id);
        IF NEW.quantity > v_counts.available - v_counts.awaiting THEN
            RAISE EXCEPTION 'остаток товара % (%) больше обеспеченного ключами (свободно %, ждут выдачи %)',
                NEW.id, NEW.quantity, v_counts.available, v_counts.awaiting;
        END IF;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER products_digital_stock_guard
    BEFORE UPDATE OF quantity, fulfillment_type ON products
    FOR EACH ROW EXECUTE FUNCTION products_digital_stock_guard();

-- ---------- 8. apply_paid_order: проверка запаса ключей (R1) ----------
--
-- Как в 0003, плюс: для товара, у позиции которого fulfillment_type = 'digital_keys', после
-- блокировки строки товара требуется A − W ≥ needed. Иначе — прежний путь out_of_stock
-- (деньги учтены вызывающей функцией, заказ requires_action, остаток не списан, задач нет;
-- решение — admin_retry_paid_order после пополнения или возврат). Причина — прежняя
-- 'insufficient_stock', формат ответа не меняется. CREATE OR REPLACE сохраняет владельца и права,
-- но сбрасывает search_path — он закрепляется заново в разделе 10.

CREATE OR REPLACE FUNCTION apply_paid_order(p_order_id INTEGER) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_status TEXT;
    v_problems JSONB := '[]'::jsonb;
    v_items INTEGER := 0;
    v_tracked INTEGER := 0;
    v_updated INTEGER;
    v_counts RECORD;
    r RECORD;
BEGIN
    SELECT status INTO v_status FROM orders WHERE id = p_order_id FOR UPDATE;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('error', 'order_not_found');
    END IF;
    IF v_status = 'paid' THEN
        RETURN jsonb_build_object('status', 'already_paid');
    END IF;
    IF v_status NOT IN ('pending', 'requires_action') THEN
        RETURN jsonb_build_object('error', 'invalid_order_status');
    END IF;

    FOR r IN
        SELECT p.id, p.quantity, p.is_for_sale, p.inventory_mode, n.needed, n.digital
        FROM (
            SELECT product_id, sum(quantity)::BIGINT AS needed,
                   bool_or(fulfillment_type = 'digital_keys') AS digital
            FROM order_items
            WHERE order_id = p_order_id
            GROUP BY product_id
        ) n
        JOIN products p ON p.id = n.product_id
        ORDER BY p.id
        FOR UPDATE OF p
    LOOP
        v_items := v_items + 1;
        IF NOT r.is_for_sale THEN
            v_problems := v_problems || jsonb_build_object('product_id', r.id, 'reason', 'not_for_sale');
        ELSIF r.inventory_mode = 'tracked' AND r.quantity < r.needed THEN
            v_problems := v_problems || jsonb_build_object('product_id', r.id, 'reason', 'insufficient_stock');
        ELSIF r.digital THEN
            -- Строка товара уже заблокирована: импорт, изъятие и ручные изменения остатка ждут.
            SELECT * INTO v_counts FROM digital_key_stock_counts(r.id);
            IF v_counts.available - v_counts.awaiting < r.needed THEN
                v_problems := v_problems || jsonb_build_object('product_id', r.id, 'reason', 'insufficient_stock');
            END IF;
        END IF;
        IF r.inventory_mode = 'tracked' THEN
            v_tracked := v_tracked + 1;
        END IF;
    END LOOP;

    IF v_items = 0 THEN
        RETURN jsonb_build_object('error', 'order_has_no_items');
    END IF;

    IF jsonb_array_length(v_problems) > 0 THEN
        UPDATE orders SET status = 'requires_action' WHERE id = p_order_id;
        RETURN jsonb_build_object('error', 'out_of_stock', 'order_id', p_order_id,
                                  'status', 'requires_action', 'problems', v_problems);
    END IF;

    UPDATE products p
    SET quantity = p.quantity - n.needed
    FROM (
        SELECT product_id, sum(quantity)::BIGINT AS needed
        FROM order_items
        WHERE order_id = p_order_id
        GROUP BY product_id
    ) n
    WHERE p.id = n.product_id
      AND p.inventory_mode = 'tracked'
      AND p.quantity >= n.needed;
    GET DIAGNOSTICS v_updated = ROW_COUNT;
    IF v_updated <> v_tracked THEN
        RAISE EXCEPTION 'остатки заказа % изменились во время оплаты', p_order_id;
    END IF;

    UPDATE orders SET status = 'paid', paid_at = now() WHERE id = p_order_id;

    INSERT INTO fulfillments (order_item_id, fulfillment_type)
    SELECT id, fulfillment_type FROM order_items WHERE order_id = p_order_id ORDER BY id
    ON CONFLICT (order_item_id) DO NOTHING;

    RETURN jsonb_build_object('order_id', p_order_id, 'status', 'paid');
END
$$;

-- ---------- 9. функции ----------

-- 9.1 Назначение ключей задаче (worker, внутри транзакции и SAVEPOINT обработчика).
-- Блокировки: задача (уже у worker) → успешный платёж заказа FOR SHARE → advisory(товар) → ключи.
CREATE FUNCTION digital_keys_assign(p_fulfillment_id BIGINT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_task RECORD;
    v_assigned BIGINT;
    v_payment BIGINT;
    v_ids BIGINT[];
    v_available BIGINT;
BEGIN
    SELECT f.id, f.status, f.fulfillment_type, f.refund_hold_released_at,
           i.id AS order_item_id, i.order_id, i.product_id, i.quantity, o.status AS order_status
    INTO v_task
    FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id JOIN orders o ON o.id = i.order_id
    WHERE f.id = p_fulfillment_id
    FOR UPDATE OF f;
    IF NOT FOUND OR v_task.fulfillment_type <> 'digital_keys' THEN
        RAISE EXCEPTION 'задача % не является выдачей цифровых ключей', p_fulfillment_id;
    END IF;
    IF v_task.order_status <> 'paid' THEN
        RETURN jsonb_build_object('result', 'not_paid');
    END IF;
    IF v_task.status <> 'pending' THEN
        RETURN jsonb_build_object('result', 'not_pending', 'status', v_task.status);
    END IF;

    SELECT count(*) INTO v_assigned FROM digital_keys WHERE fulfillment_id = p_fulfillment_id AND status = 'assigned';
    IF v_assigned = v_task.quantity THEN
        RETURN jsonb_build_object('result', 'already_assigned', 'count', v_assigned);
    ELSIF v_assigned <> 0 THEN
        RAISE EXCEPTION 'задача %: назначено % ключей из % — нарушена атомарность', p_fulfillment_id,
            v_assigned, v_task.quantity;
    END IF;

    -- Запрос возврата по платежу, который оплатил заказ, останавливает выдачу (R2).
    -- FOR SHARE сериализует с request_refund / refunds_guard (они берут FOR UPDATE).
    SELECT p.id INTO v_payment FROM payments p
    WHERE p.order_id = v_task.order_id AND p.status = 'succeeded'
    FOR SHARE;
    IF v_payment IS NOT NULL AND v_task.refund_hold_released_at IS NULL
       AND EXISTS (SELECT 1 FROM refunds r WHERE r.payment_id = v_payment AND r.status <> 'canceled') THEN
        RETURN jsonb_build_object('result', 'refund_hold');
    END IF;

    -- Выдачи и изъятия одного товара — по очереди (без SKIP LOCKED: занятые ключи не «исчезают»).
    PERFORM pg_advisory_xact_lock(1684368249, v_task.product_id);

    SELECT array_agg(id ORDER BY id) INTO v_ids FROM (
        SELECT id FROM digital_keys
        WHERE product_id = v_task.product_id AND status = 'available'
        ORDER BY id
        LIMIT v_task.quantity
        FOR UPDATE
    ) k;
    IF coalesce(cardinality(v_ids), 0) < v_task.quantity THEN
        SELECT count(*) INTO v_available FROM digital_keys
        WHERE product_id = v_task.product_id AND status = 'available';
        INSERT INTO audit_log (actor, action, order_id, details)
        VALUES ('fulfillment', 'digital_keys.shortage', v_task.order_id,
                jsonb_build_object('fulfillment_id', p_fulfillment_id, 'product_id', v_task.product_id,
                                   'need', v_task.quantity, 'available', v_available));
        RETURN jsonb_build_object('result', 'insufficient', 'need', v_task.quantity, 'available', v_available);
    END IF;

    UPDATE digital_keys
    SET status = 'assigned', fulfillment_id = p_fulfillment_id, order_item_id = v_task.order_item_id,
        assigned_at = now()
    WHERE id = ANY(v_ids);

    INSERT INTO audit_log (actor, action, order_id, details)
    VALUES ('fulfillment', 'digital_keys.assigned', v_task.order_id,
            jsonb_build_object('fulfillment_id', p_fulfillment_id, 'product_id', v_task.product_id,
                               'count', v_task.quantity));
    RETURN jsonb_build_object('result', 'assigned', 'count', v_task.quantity);
END
$$;

-- 9.2 Результаты выдачи для владельца секрета заказа (веб-роль). NULL — нет заказа или неверный
-- секрет (не различаются). Шифротексты — только оплаченного заказа и выполненной задачи.
CREATE FUNCTION order_delivery_view(p_order_id INTEGER, p_token_sha256 BYTEA) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_order RECORD;
    v_items JSONB;
    v_revealed BIGINT;
BEGIN
    SELECT o.id, o.status INTO v_order
    FROM orders o JOIN order_access a ON a.order_id = o.id
    WHERE o.id = p_order_id AND a.token_sha256 = p_token_sha256;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;

    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'order_item_id', i.id, 'product_id', i.product_id, 'name', i.product_name,
               'fulfillment_type', i.fulfillment_type, 'quantity', i.quantity,
               'fulfillment_status', f.status,
               'keys', CASE WHEN v_order.status = 'paid' AND f.status = 'fulfilled'
                                 AND i.fulfillment_type = 'digital_keys' THEN (
                            SELECT coalesce(jsonb_agg(jsonb_build_object(
                                       'public_id', k.public_id, 'product_id', k.product_id,
                                       'enc_key_id', k.enc_key_id, 'nonce', encode(k.nonce, 'hex'),
                                       'ciphertext', encode(k.ciphertext, 'hex')) ORDER BY k.id), '[]'::jsonb)
                            FROM digital_keys k
                            WHERE k.fulfillment_id = f.id AND k.status = 'assigned')
                        END) ORDER BY i.id), '[]'::jsonb)
    INTO v_items
    FROM order_items i LEFT JOIN fulfillments f ON f.order_item_id = i.id
    WHERE i.order_id = p_order_id;

    SELECT coalesce(sum(jsonb_array_length(item->'keys')), 0) INTO v_revealed
    FROM jsonb_array_elements(v_items) item WHERE jsonb_typeof(item->'keys') = 'array';
    IF v_revealed > 0 THEN
        INSERT INTO audit_log (actor, action, order_id, details)
        VALUES ('delivery', 'digital_keys.revealed', p_order_id, jsonb_build_object('count', v_revealed));
    END IF;

    RETURN jsonb_build_object('order_id', v_order.id, 'order_status', v_order.status, 'items', v_items);
END
$$;

-- 9.3 Проверочные значения ключей шифрования (не секрет): веб и администратор сверяют
-- настроенные у себя ключи с реестром.
CREATE FUNCTION encryption_key_check_values() RETURNS JSONB
LANGUAGE sql STABLE SECURITY DEFINER AS $$
    SELECT coalesce(jsonb_object_agg(purpose || ':' || key_id, encode(check_value, 'hex')), '{}'::jsonb)
    FROM encryption_key_registry WHERE retired_at IS NULL
$$;

-- 9.4 Проверка полномочий администратора (вызывается из административных функций).
--   * группа администраторов задана (shop_security_settings) и не может входить сама (NOLOGIN);
--   * вошедший пользователь (session_user — SET ROLE его не меняет) не суперпользователь и не
--     владелец таблиц магазина (мигратор — не обычный путь; аварийный порядок — DIGITAL_KEYS.md);
--   * он член группы администраторов С НАСЛЕДОВАНИЕМ прав (pg_has_role … 'USAGE');
--   * у него есть EXECUTE на вызываемую функцию.
-- Ни одна проверка не опирается на имя или префикс вошедшей роли.
CREATE FUNCTION shop_require_admin(p_function REGPROCEDURE) RETURNS TEXT
LANGUAGE plpgsql AS $$
DECLARE
    v_group TEXT;
    v_session TEXT := session_user;
BEGIN
    SELECT admin_group INTO v_group FROM shop_security_settings;
    IF v_group IS NULL OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_group AND NOT rolcanlogin) THEN
        RAISE EXCEPTION 'группа администраторов магазина не настроена (deploy/db_roles.py)';
    END IF;
    IF (SELECT rolsuper FROM pg_roles WHERE rolname = v_session) THEN
        RAISE EXCEPTION 'административные действия под суперпользователем запрещены';
    END IF;
    IF shop_is_owner_role(v_session) THEN
        RAISE EXCEPTION 'административные действия под владельцем схемы запрещены: войдите персональной ролью администратора';
    END IF;
    IF NOT pg_has_role(v_session, v_group, 'USAGE') THEN
        RAISE EXCEPTION 'нужна учётная запись администратора магазина';
    END IF;
    IF NOT has_function_privilege(v_session, p_function, 'EXECUTE') THEN
        RAISE EXCEPTION 'нет права на это действие';
    END IF;
    RETURN v_session;
END
$$;

CREATE FUNCTION shop_is_owner_role(p_role TEXT) RETURNS BOOLEAN
LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner
                   WHERE c.oid = 'digital_keys'::regclass AND r.rolname = p_role)
$$;

-- 9.5 Ядро административных действий: SECURITY INVOKER, EXECUTE не выдаётся никому.
-- Вызываются только из admin_*-обёрток (после shop_require_admin) и владельцем (тесты).

CREATE FUNCTION digital_keys_import_core(
    p_product_id INTEGER, p_source_label TEXT, p_public_ids TEXT[], p_enc_key_ids TEXT[],
    p_nonces_hex TEXT[], p_ciphertexts_hex TEXT[], p_fingerprints_hex TEXT[],
    p_fingerprint_key_id TEXT, p_skip_duplicates BOOLEAN, p_actor TEXT
) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_product RECORD;
    v_n INTEGER := coalesce(cardinality(p_public_ids), 0);
    v_seen BYTEA[] := '{}';
    v_fp BYTEA;
    v_in_batch INTEGER[] := '{}';
    v_existing INTEGER[] := '{}';
    v_insert INTEGER[] := '{}';
    v_batch BIGINT;
    v_counts RECORD;
    v_added INTEGER := 0;
    v_fp_version TEXT;
    i INTEGER;
BEGIN
    IF v_n < 1 OR v_n > 10000 THEN
        RAISE EXCEPTION 'в партии должно быть от 1 до 10000 ключей';
    END IF;
    IF cardinality(p_enc_key_ids) <> v_n OR cardinality(p_nonces_hex) <> v_n
       OR cardinality(p_ciphertexts_hex) <> v_n OR cardinality(p_fingerprints_hex) <> v_n THEN
        RAISE EXCEPTION 'массивы партии разной длины';
    END IF;

    SELECT id, product_type, fulfillment_type, quantity INTO v_product
    FROM products WHERE id = p_product_id FOR UPDATE;
    IF NOT FOUND OR v_product.product_type <> 'digital_key' THEN
        RAISE EXCEPTION 'ключи загружаются только для существующего товара типа digital_key';
    END IF;
    IF EXISTS (SELECT 1 FROM unnest(p_enc_key_ids) k
               WHERE NOT EXISTS (SELECT 1 FROM encryption_key_registry r
                                 WHERE r.purpose = 'encryption' AND r.key_id = k AND r.retired_at IS NULL))
       OR NOT EXISTS (SELECT 1 FROM encryption_key_registry r
                      WHERE r.purpose = 'fingerprint' AND r.key_id = p_fingerprint_key_id AND r.retired_at IS NULL) THEN
        RAISE EXCEPTION 'ключ шифрования или отпечатков не зарегистрирован (verify-keyring)';
    END IF;

    -- Одна версия секрета отпечатков на весь склад. Отпечатки одного кода под разными секретами
    -- различны, и UNIQUE(fingerprint) повтор бы не обнаружил. Ротация отпечатков (пересчёт всех
    -- отпечатков) в PR5 не реализована, поэтому импорт под другой версией отклоняется, пока в базе
    -- есть ключи (любого статуса) с прежней версией. Глобальная блокировка импорта (после строки
    -- товара; другие пути её не берут) исключает одновременный первый импорт под двумя версиями и
    -- выстраивает импорты одного кода в разные товары: второй видит первый и получает отчёт о дубликате.
    PERFORM pg_advisory_xact_lock(1684368250, 0);
    SELECT fingerprint_key_id INTO v_fp_version FROM digital_keys
    WHERE fingerprint_key_id <> p_fingerprint_key_id LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'на складе есть ключи с отпечатками версии %; импорт под версией % запрещён (ротация секрета отпечатков не поддерживается)',
            v_fp_version, p_fingerprint_key_id;
    END IF;

    FOR i IN 1..v_n LOOP
        v_fp := decode(p_fingerprints_hex[i], 'hex');
        IF v_fp = ANY(v_seen) THEN
            v_in_batch := v_in_batch || i;
        ELSIF EXISTS (SELECT 1 FROM digital_keys WHERE fingerprint = v_fp) THEN
            v_existing := v_existing || i;
            v_seen := v_seen || v_fp;
        ELSE
            v_insert := v_insert || i;
            v_seen := v_seen || v_fp;
        END IF;
    END LOOP;

    IF cardinality(v_insert) = 0 OR (NOT p_skip_duplicates AND cardinality(v_in_batch) + cardinality(v_existing) > 0) THEN
        RETURN jsonb_build_object('status', 'rejected', 'submitted', v_n, 'inserted', 0,
                                  'duplicates_in_batch', v_in_batch, 'duplicates_existing', v_existing);
    END IF;

    -- Остаток товара с автоматической выдачей растёт вместе с ключами, но не выше обеспеченного:
    -- A_new − W − Q_old (у товаров с завышенным старым остатком прибавка меньше или 0).
    -- Строка товара заблокирована: A и W другие транзакции сейчас не увеличат сверх нашего учёта.
    IF v_product.fulfillment_type = 'digital_keys' THEN
        SELECT * INTO v_counts FROM digital_key_stock_counts(p_product_id);
        v_added := GREATEST(0, LEAST(cardinality(v_insert)::BIGINT,
                                     v_counts.available + cardinality(v_insert) - v_counts.awaiting
                                     - v_product.quantity))::INTEGER;
    END IF;

    INSERT INTO digital_key_batches (product_id, source_label, imported_by, submitted, inserted,
                                     duplicates_in_batch, duplicates_existing, quantity_added)
    VALUES (p_product_id, p_source_label, p_actor, v_n, cardinality(v_insert),
            cardinality(v_in_batch), cardinality(v_existing), v_added)
    RETURNING id INTO v_batch;

    INSERT INTO digital_keys (public_id, product_id, batch_id, enc_key_id, nonce, ciphertext, fingerprint,
                              fingerprint_key_id)
    SELECT p_public_ids[j]::uuid, p_product_id, v_batch, p_enc_key_ids[j], decode(p_nonces_hex[j], 'hex'),
           decode(p_ciphertexts_hex[j], 'hex'), decode(p_fingerprints_hex[j], 'hex'), p_fingerprint_key_id
    FROM unnest(v_insert) j;

    IF v_added > 0 THEN
        UPDATE products SET quantity = quantity + v_added WHERE id = p_product_id;
    END IF;

    INSERT INTO audit_log (actor, action, details)
    VALUES ('admin', 'digital_keys.imported',
            jsonb_build_object('product_id', p_product_id, 'batch_id', v_batch, 'by', p_actor,
                               'inserted', cardinality(v_insert), 'quantity_added', v_added,
                               'duplicates_in_batch', cardinality(v_in_batch),
                               'duplicates_existing', cardinality(v_existing)));
    RETURN jsonb_build_object('status', 'imported', 'batch_id', v_batch, 'submitted', v_n,
                              'inserted', cardinality(v_insert), 'quantity_added', v_added,
                              'duplicates_in_batch', v_in_batch, 'duplicates_existing', v_existing);
END
$$;

CREATE FUNCTION digital_keys_withdraw_core(p_public_id UUID, p_reason TEXT, p_actor TEXT) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_product_id INTEGER;
    v_product RECORD;
    v_counts RECORD;
    v_key RECORD;
BEGIN
    IF p_reason IS NULL OR char_length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'укажите причину изъятия';
    END IF;
    SELECT product_id INTO v_product_id FROM digital_keys WHERE public_id = p_public_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'ключ не найден';
    END IF;
    SELECT id, fulfillment_type, quantity INTO v_product FROM products WHERE id = v_product_id FOR UPDATE;
    PERFORM pg_advisory_xact_lock(1684368249, v_product_id);
    SELECT id, status INTO v_key FROM digital_keys WHERE public_id = p_public_id FOR UPDATE;
    IF v_key.status <> 'available' THEN
        RAISE EXCEPTION 'изъять можно только свободный ключ (сейчас %)', v_key.status;
    END IF;
    IF v_product.fulfillment_type = 'digital_keys' THEN
        SELECT * INTO v_counts FROM digital_key_stock_counts(v_product_id);
        IF v_counts.available - 1 < v_counts.awaiting + v_product.quantity THEN
            RAISE EXCEPTION 'изъятие нарушит обеспеченность остатка: сначала уменьшите остаток товара % (свободно %, ждут %, остаток %)',
                v_product_id, v_counts.available, v_counts.awaiting, v_product.quantity;
        END IF;
    END IF;
    UPDATE digital_keys SET status = 'withdrawn', withdrawn_at = now(), withdraw_reason = left(p_reason, 200)
    WHERE id = v_key.id;
    INSERT INTO audit_log (actor, action, details)
    VALUES ('admin', 'digital_keys.withdrawn',
            jsonb_build_object('product_id', v_product_id, 'key', p_public_id, 'by', p_actor,
                               'reason', left(p_reason, 200)));
    RETURN jsonb_build_object('status', 'withdrawn', 'product_id', v_product_id);
END
$$;

CREATE FUNCTION fulfillment_resume_core(p_fulfillment_id BIGINT, p_reason TEXT, p_release_refund_hold BOOLEAN,
                                        p_actor TEXT) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_task RECORD;
    v_available BIGINT;
BEGIN
    IF p_reason IS NULL OR char_length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'укажите основание';
    END IF;
    SELECT f.id, f.status, f.fulfillment_type, i.order_id, i.product_id, i.quantity
    INTO v_task
    FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id
    WHERE f.id = p_fulfillment_id
    FOR UPDATE OF f;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'задача % не найдена', p_fulfillment_id;
    END IF;
    IF v_task.status NOT IN ('requires_action', 'failed') THEN
        RAISE EXCEPTION 'возобновить можно задачу в requires_action или failed (сейчас %)', v_task.status;
    END IF;
    IF v_task.fulfillment_type = 'digital_keys' THEN
        PERFORM pg_advisory_xact_lock(1684368249, v_task.product_id);
        SELECT count(*) INTO v_available FROM digital_keys
        WHERE product_id = v_task.product_id AND status = 'available';
        IF v_available < v_task.quantity THEN
            RAISE EXCEPTION 'свободных ключей % меньше нужного % — сначала импортируйте ключи', v_available, v_task.quantity;
        END IF;
    END IF;
    UPDATE fulfillments
    SET status = 'pending', attempts = 0, next_attempt_at = now(), last_error = NULL,
        status_note = left('Возобновлено администратором: ' || p_reason, 300),
        refund_hold_released_at = CASE WHEN p_release_refund_hold THEN now() ELSE refund_hold_released_at END,
        updated_at = now()
    WHERE id = p_fulfillment_id;
    INSERT INTO audit_log (actor, action, order_id, details)
    VALUES ('admin', 'fulfillment.resumed', v_task.order_id,
            jsonb_build_object('fulfillment_id', p_fulfillment_id, 'by', p_actor, 'reason', left(p_reason, 200),
                               'release_refund_hold', p_release_refund_hold));
    RETURN jsonb_build_object('status', 'pending');
END
$$;

CREATE FUNCTION fulfillment_cancel_core(p_fulfillment_id BIGINT, p_reason TEXT, p_actor TEXT) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_task RECORD;
BEGIN
    IF p_reason IS NULL OR char_length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'укажите причину отмены';
    END IF;
    SELECT f.id, f.status, i.order_id INTO v_task
    FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id
    WHERE f.id = p_fulfillment_id
    FOR UPDATE OF f;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'задача % не найдена', p_fulfillment_id;
    END IF;
    IF v_task.status NOT IN ('pending', 'requires_action', 'failed') THEN
        RAISE EXCEPTION 'отменить нельзя: задача уже % (выданное не отзывается)', v_task.status;
    END IF;
    -- Остаток товара и денежный учёт не меняются (R3).
    UPDATE fulfillments
    SET status = 'cancelled', cancel_reason = left(p_reason, 200), next_attempt_at = NULL,
        status_note = 'Отменено администратором', updated_at = now()
    WHERE id = p_fulfillment_id;
    INSERT INTO audit_log (actor, action, order_id, details)
    VALUES ('admin', 'fulfillment.cancelled', v_task.order_id,
            jsonb_build_object('fulfillment_id', p_fulfillment_id, 'by', p_actor, 'reason', left(p_reason, 200)));
    RETURN jsonb_build_object('status', 'cancelled');
END
$$;

CREATE FUNCTION digital_stock_reconcile_core(p_product_id INTEGER, p_actor TEXT) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_product RECORD;
    v_counts RECORD;
    v_new INTEGER;
BEGIN
    SELECT id, fulfillment_type, quantity INTO v_product FROM products WHERE id = p_product_id FOR UPDATE;
    IF NOT FOUND OR v_product.fulfillment_type <> 'digital_keys' THEN
        RAISE EXCEPTION 'сверка остатка — только для товара с выдачей digital_keys';
    END IF;
    SELECT * INTO v_counts FROM digital_key_stock_counts(p_product_id);
    v_new := LEAST(v_product.quantity::BIGINT, GREATEST(0, v_counts.available - v_counts.awaiting))::INTEGER;
    IF v_new < v_product.quantity THEN
        UPDATE products SET quantity = v_new WHERE id = p_product_id;
    END IF;
    INSERT INTO audit_log (actor, action, details)
    VALUES ('admin', 'digital_keys.stock_reconciled',
            jsonb_build_object('product_id', p_product_id, 'by', p_actor, 'old_quantity', v_product.quantity,
                               'new_quantity', v_new, 'available', v_counts.available, 'awaiting', v_counts.awaiting));
    RETURN jsonb_build_object('product_id', p_product_id, 'old_quantity', v_product.quantity, 'new_quantity', v_new);
END
$$;

CREATE FUNCTION encryption_key_register_core(p_purpose TEXT, p_key_id TEXT, p_check_value_hex TEXT, p_actor TEXT)
RETURNS TEXT
LANGUAGE plpgsql AS $$
DECLARE
    v_existing BYTEA;
BEGIN
    SELECT check_value INTO v_existing FROM encryption_key_registry WHERE purpose = p_purpose AND key_id = p_key_id;
    IF FOUND THEN
        IF v_existing <> decode(p_check_value_hex, 'hex') THEN
            RAISE EXCEPTION 'ключ %/% уже зарегистрирован с ДРУГИМ проверочным значением — настроен не тот ключ',
                p_purpose, p_key_id;
        END IF;
        RETURN 'already_registered';
    END IF;
    INSERT INTO encryption_key_registry (purpose, key_id, check_value, registered_by)
    VALUES (p_purpose, p_key_id, decode(p_check_value_hex, 'hex'), p_actor);
    INSERT INTO audit_log (actor, action, details)
    VALUES ('admin', 'encryption_key.registered',
            jsonb_build_object('purpose', p_purpose, 'key_id', p_key_id, 'by', p_actor));
    RETURN 'registered';
END
$$;

CREATE FUNCTION digital_keys_keyring_sample_core() RETURNS JSONB
LANGUAGE sql STABLE AS $$
    SELECT coalesce(jsonb_agg(jsonb_build_object(
               'enc_key_id', s.enc_key_id, 'count', s.cnt, 'public_id', k.public_id, 'product_id', k.product_id,
               'nonce', encode(k.nonce, 'hex'), 'ciphertext', encode(k.ciphertext, 'hex'))), '[]'::jsonb)
    FROM (SELECT enc_key_id, count(*) AS cnt, min(id) AS first_id FROM digital_keys GROUP BY enc_key_id) s
    JOIN digital_keys k ON k.id = s.first_id
$$;

-- 9.6 Административные обёртки (SECURITY DEFINER, EXECUTE — только группе администраторов).

CREATE FUNCTION admin_import_digital_keys(
    p_product_id INTEGER, p_source_label TEXT, p_public_ids TEXT[], p_enc_key_ids TEXT[],
    p_nonces_hex TEXT[], p_ciphertexts_hex TEXT[], p_fingerprints_hex TEXT[],
    p_fingerprint_key_id TEXT, p_skip_duplicates BOOLEAN
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN digital_keys_import_core(
        p_product_id, p_source_label, p_public_ids, p_enc_key_ids, p_nonces_hex, p_ciphertexts_hex,
        p_fingerprints_hex, p_fingerprint_key_id, p_skip_duplicates,
        shop_require_admin('admin_import_digital_keys(integer, text, text[], text[], text[], text[], text[], text, boolean)'::regprocedure));
END
$$;

CREATE FUNCTION admin_withdraw_digital_key(p_public_id UUID, p_reason TEXT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN digital_keys_withdraw_core(p_public_id, p_reason,
        shop_require_admin('admin_withdraw_digital_key(uuid, text)'::regprocedure));
END
$$;

CREATE FUNCTION admin_resume_fulfillment(p_fulfillment_id BIGINT, p_reason TEXT, p_release_refund_hold BOOLEAN)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN fulfillment_resume_core(p_fulfillment_id, p_reason, p_release_refund_hold,
        shop_require_admin('admin_resume_fulfillment(bigint, text, boolean)'::regprocedure));
END
$$;

CREATE FUNCTION admin_cancel_fulfillment(p_fulfillment_id BIGINT, p_reason TEXT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN fulfillment_cancel_core(p_fulfillment_id, p_reason,
        shop_require_admin('admin_cancel_fulfillment(bigint, text)'::regprocedure));
END
$$;

CREATE FUNCTION admin_reconcile_digital_stock(p_product_id INTEGER) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN digital_stock_reconcile_core(p_product_id,
        shop_require_admin('admin_reconcile_digital_stock(integer)'::regprocedure));
END
$$;

CREATE FUNCTION admin_register_encryption_key(p_purpose TEXT, p_key_id TEXT, p_check_value_hex TEXT) RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN encryption_key_register_core(p_purpose, p_key_id, p_check_value_hex,
        shop_require_admin('admin_register_encryption_key(text, text, text)'::regprocedure));
END
$$;

CREATE FUNCTION admin_digital_keys_keyring_sample() RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    PERFORM shop_require_admin('admin_digital_keys_keyring_sample()'::regprocedure);
    RETURN digital_keys_keyring_sample_core();
END
$$;

CREATE FUNCTION admin_whoami() RETURNS TEXT
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN shop_require_admin('admin_whoami()'::regprocedure);
END
$$;

-- ---------- 10. доверенный search_path и права на функции ----------

DO $$
DECLARE
    shop_schema TEXT := current_schema();
    fn REGPROCEDURE;
BEGIN
    IF shop_schema IS NULL OR shop_schema LIKE 'pg\_temp%' OR shop_schema = 'pg_catalog' THEN
        RAISE EXCEPTION 'миграция должна выполняться со схемой магазина в search_path (сейчас: %)', shop_schema;
    END IF;
    FOR fn IN
        SELECT p.oid::REGPROCEDURE FROM pg_proc p
        WHERE p.pronamespace = shop_schema::REGNAMESPACE
          AND p.proname IN (
              'digital_key_stock_counts', 'digital_keys_task_complete', 'digital_keys_guard', 'digital_keys_protect_truncate',
              'digital_key_records_protect', 'fulfillments_guard', 'products_digital_stock_guard', 'apply_paid_order',
              'digital_keys_assign', 'order_delivery_view', 'encryption_key_check_values', 'shop_require_admin',
              'shop_is_owner_role', 'digital_keys_import_core', 'digital_keys_withdraw_core',
              'fulfillment_resume_core', 'fulfillment_cancel_core', 'digital_stock_reconcile_core',
              'encryption_key_register_core', 'digital_keys_keyring_sample_core', 'admin_import_digital_keys',
              'admin_withdraw_digital_key', 'admin_resume_fulfillment', 'admin_cancel_fulfillment',
              'admin_reconcile_digital_stock', 'admin_register_encryption_key', 'admin_digital_keys_keyring_sample',
              'admin_whoami')
    LOOP
        EXECUTE format('ALTER FUNCTION %s SET search_path = pg_catalog, %I, pg_temp', fn, shop_schema);
    END LOOP;
    FOR fn IN
        SELECT p.oid::REGPROCEDURE FROM pg_proc p
        WHERE p.pronamespace = shop_schema::REGNAMESPACE
          AND p.proname IN (
              'digital_key_stock_counts', 'digital_keys_task_complete', 'apply_paid_order', 'digital_keys_assign',
              'order_delivery_view', 'encryption_key_check_values', 'shop_require_admin', 'shop_is_owner_role',
              'digital_keys_import_core', 'digital_keys_withdraw_core', 'fulfillment_resume_core',
              'fulfillment_cancel_core', 'digital_stock_reconcile_core', 'encryption_key_register_core',
              'digital_keys_keyring_sample_core', 'admin_import_digital_keys', 'admin_withdraw_digital_key',
              'admin_resume_fulfillment', 'admin_cancel_fulfillment', 'admin_reconcile_digital_stock',
              'admin_register_encryption_key', 'admin_digital_keys_keyring_sample', 'admin_whoami')
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', fn);
    END LOOP;
END
$$;

-- ================= аккаунты покупателей (миграция 0005) =================

-- 0005 — аккаунты покупателей, вариант P1 (PR6).
--
-- Архитектура: PR6_DESIGN_REV4 → REV5 → REV5_ADDENDUM → REV5_ADDENDUM2 (более поздний документ главнее).
--
-- Что делает:
--   1. auth_settings      — флаги accounts_enabled / registration_open (по умолчанию ВЫКЛЮЧЕНЫ), окно и лимит
--                           попыток, сроки сессий, стоимость bcrypt и фиктивный хэш той же стоимости.
--   2. customers          — покупатели: email_norm, bcrypt-хэш ДОКАЗАТЕЛЬСТВА пароля (HMAC с перцем
--                           вычисляет приложение; пароль в базу не передаётся), версия перца, auth_epoch.
--   3. customer_sessions  — сессии: только SHA-256 токена; эпоха учётных данных на момент входа.
--   4. customer_orders    — привязка НОВОГО заказа к покупателю (не более одного раза).
--   5. auth_throttle      — счётчики попыток проверки пароля (фиксированное окно), ключ — SHA-256.
--   6. Триггеры целостности и функции (SECURITY DEFINER для вызова ролями; ядра — SECURITY INVOKER).
--
-- Не меняет: таблицы, функции и данные 0000–0004; гостевой access_token (order_access, PR3),
-- платежи PR3 и выдачу ключей PR5. Права ролям (GRANT EXECUTE) выдаёт deploy/db_roles.py, не миграция.
--
-- Предусловие: расширение pgcrypto установлено в схеме ext_pgcrypto и роль миграции имеет на неё USAGE
-- (deploy/db_roles.py --part extensions, DB_ROLES.md, шаг 4а). Функции pgcrypto вызываются только
-- с явной схемой (ext_pgcrypto.crypt / gen_salt / gen_random_bytes); в search_path её нет.
--
-- Порядок блокировок во всех функциях (REV5 §2.1, ADDENDUM §1.2, ADDENDUM2 §1.2, §3.2):
--   (1) строка auth_throttle → (2) строка customers → (3) строки customer_sessions.
--   customers: FOR NO KEY UPDATE — вход, смена пароля, «выйти везде», отключение, отзыв сессий;
--              FOR UPDATE — освобождение email (меняется уникальный email_norm);
--              FOR SHARE — привязка заказа (затем ОТДЕЛЬНЫМ оператором повторная проверка сессии);
--              без блокировки — resolve и просмотры.
--   Все блокировки берутся до bcrypt, в подтранзакции с lock_timeout = 2s; ожидание → 'busy'
--   (учёт попытки откатывается вместе с подтранзакцией, пароль не проверялся).
--   auth_session_touch блокирует только строку сессии и только с SKIP LOCKED (никого не ждёт).
--
-- Отказы, которые учитываются в счётчиках, — значения результата (JSONB {"result": ...}), а не RAISE:
-- иначе вызывающий откатил бы учёт. RAISE — только при неверном формате входных данных и уровне
-- изоляции, отличном от READ COMMITTED (от пароля не зависит).
--
-- Флаг accounts_enabled. Точка переключения — COMMIT изменения auth_settings.
--   * Действительность сессии проверяется вместе с флагом ОДНИМ оператором (customer_session_customer,
--     привязка заказа (c)), в смене пароля, «выйти везде» и привязке — после всех ожиданий блокировок.
--     Это точка линеаризации этих операций: выключенный флаг в тот же момент делает недействительными
--     все сессии для resolve, просмотров, touch, привязки, смены пароля и «выйти везде».
--   * Вход и регистрация линеаризуются в момент чтения флага в начале функции. Если выключение
--     зафиксировано позже, созданные ими сессия и аккаунт не отличаются от созданных мгновением раньше:
--     пока флаг выключен, сессия не действует (её проверяет каждый вызов); после повторного включения
--     действует, как и все прежние сессии (ADDENDUM2 §2: выключение флага не удаляет данные).
--   * Строка auth_settings не блокируется: модель блокировок P1 не меняется.
--
-- Время. now() (начало транзакции) — для записываемых меток: created_at, окно счётчика, сроки новой
-- сессии, аудит. clock_timestamp() — для проверки сроков сессии (истёкшая во время ожидания блокировки
-- сессия недействительна) и для продления простоя.

-- ---------- 0. предусловия ----------

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace
                   WHERE e.extname = 'pgcrypto' AND n.nspname = 'ext_pgcrypto') THEN
        RAISE EXCEPTION '0005: нужно расширение pgcrypto в схеме ext_pgcrypto (deploy/db_roles.py --part extensions, DB_ROLES.md, шаг 4а)';
    END IF;
    IF NOT has_schema_privilege('ext_pgcrypto', 'USAGE') THEN
        RAISE EXCEPTION '0005: у роли миграции (%) нет USAGE на схему ext_pgcrypto', current_user;
    END IF;
END
$$;

-- ---------- 1. настройки ----------

CREATE TABLE auth_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE,
    accounts_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    registration_open BOOLEAN NOT NULL DEFAULT FALSE,
    login_window INTERVAL NOT NULL DEFAULT '15 minutes',
    login_limit INTEGER NOT NULL DEFAULT 10,
    session_idle INTERVAL NOT NULL DEFAULT '30 days',
    session_absolute INTERVAL NOT NULL DEFAULT '90 days',
    session_touch_interval INTERVAL NOT NULL DEFAULT '10 minutes',
    bcrypt_cost SMALLINT NOT NULL DEFAULT 12,
    dummy_hash TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT auth_settings_singleton_check CHECK (singleton),
    CONSTRAINT auth_settings_login_window_check CHECK (login_window >= INTERVAL '1 second' AND login_window <= INTERVAL '1 day'),
    CONSTRAINT auth_settings_login_limit_check CHECK (login_limit BETWEEN 1 AND 1000),
    CONSTRAINT auth_settings_session_check CHECK (
        session_idle > INTERVAL '0' AND session_absolute >= session_idle AND session_absolute <= INTERVAL '366 days'
        AND session_touch_interval >= INTERVAL '0' AND session_touch_interval < session_idle),
    -- 12 — значение по умолчанию; 4 — только для изолированных тестов (не для эксплуатации).
    CONSTRAINT auth_settings_bcrypt_cost_check CHECK (bcrypt_cost BETWEEN 4 AND 15),
    -- Фиктивный хэш всегда той же стоимости, что и настоящие (одинаковое время для неизвестного email).
    CONSTRAINT auth_settings_dummy_hash_check CHECK (
        length(dummy_hash) = 60 AND left(dummy_hash, 7) = '$2a$' || lpad(bcrypt_cost::TEXT, 2, '0') || '$')
);

-- Смена стоимости и пересоздание фиктивного хэша — одна операция (в том же UPDATE).
--
-- Правило стоимости: у ВСЕХ хэшей покупателей и у фиктивного хэша одна стоимость — auth_settings.bcrypt_cost
-- (триггер customers_hash_cost_guard). Иначе время ответа отличало бы существующий email (старая стоимость)
-- от неизвестного (фиктивный хэш новой стоимости). Поэтому стоимость меняется только ДО первого покупателя
-- (значение выбирается замером Г7 до включения аккаунтов); массового пересчёта хэшей без доказательства
-- пароля нет и быть не может. Смена стоимости при существующих покупателях — отдельная будущая процедура.
-- LOCK TABLE customers IN SHARE MODE: смена стоимости ждёт незавершённые регистрации и не пропускает
-- новые до своего COMMIT — проверка «покупателей нет» и вставка с хэшем старой стоимости не разминутся.
CREATE FUNCTION auth_settings_dummy_hash() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NEW.bcrypt_cost IS NOT DISTINCT FROM OLD.bcrypt_cost AND NEW.dummy_hash IS NOT DISTINCT FROM OLD.dummy_hash THEN
            NEW.updated_at := now();
            RETURN NEW;
        END IF;
    END IF;
    -- Диапазон — до вызова bcrypt (вне диапазона gen_salt падает или считает слишком долго).
    IF NEW.bcrypt_cost IS NULL OR NEW.bcrypt_cost NOT BETWEEN 4 AND 15 THEN
        RAISE EXCEPTION 'bcrypt_cost вне диапазона 4..15';
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.bcrypt_cost IS DISTINCT FROM OLD.bcrypt_cost THEN
        LOCK TABLE customers IN SHARE MODE;
        IF EXISTS (SELECT 1 FROM customers) THEN
            RAISE EXCEPTION 'bcrypt_cost меняется только до первого покупателя: иначе время ответа различит существующие и неизвестные email';
        END IF;
    END IF;
    NEW.dummy_hash := ext_pgcrypto.crypt(
        translate(encode(ext_pgcrypto.gen_random_bytes(32), 'base64'), '+/=', '-_'),
        ext_pgcrypto.gen_salt('bf', NEW.bcrypt_cost::INTEGER));
    NEW.updated_at := now();
    RETURN NEW;
END
$$;

CREATE TRIGGER auth_settings_dummy_hash
    BEFORE INSERT OR UPDATE ON auth_settings
    FOR EACH ROW EXECUTE FUNCTION auth_settings_dummy_hash();

-- ---------- 2. покупатели ----------

CREATE TABLE customers (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email_norm TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    pepper_version SMALLINT NOT NULL,
    auth_epoch BIGINT NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    password_changed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    status_changed_at TIMESTAMPTZ,
    CONSTRAINT customers_email_norm_key UNIQUE (email_norm),
    CONSTRAINT customers_email_norm_check CHECK (
        length(email_norm) <= 254 AND email_norm = lower(email_norm) AND email_norm ~ '^[^@\s]+@[^@\s]+$'),
    CONSTRAINT customers_password_hash_check CHECK (password_hash ~ '^\$2a\$[0-9]{2}\$[./A-Za-z0-9]{53}$'),
    CONSTRAINT customers_pepper_version_check CHECK (pepper_version BETWEEN 1 AND 32767),
    CONSTRAINT customers_auth_epoch_check CHECK (auth_epoch >= 1),
    CONSTRAINT customers_status_check CHECK (status IN ('active', 'disabled'))
);

-- Стоимость каждого хэша покупателя = auth_settings.bcrypt_cost (= стоимость фиктивного хэша): нет
-- различия по времени между существующим и неизвестным email (см. auth_settings_dummy_hash).
CREATE FUNCTION customers_hash_cost_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF substring(NEW.password_hash FROM '^.2a.([0-9]{2}).')::INTEGER
       IS DISTINCT FROM (SELECT bcrypt_cost FROM auth_settings WHERE singleton) THEN
        RAISE EXCEPTION 'стоимость bcrypt хэша покупателя должна совпадать с auth_settings.bcrypt_cost';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER customers_hash_cost_guard
    BEFORE INSERT OR UPDATE OF password_hash ON customers
    FOR EACH ROW EXECUTE FUNCTION customers_hash_cost_guard();

-- ---------- 3. сессии ----------

CREATE TABLE customer_sessions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    token_sha256 BYTEA NOT NULL,
    auth_epoch BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    idle_expires_at TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT customer_sessions_token_sha256_check CHECK (octet_length(token_sha256) = 32),
    CONSTRAINT customer_sessions_token_sha256_key UNIQUE (token_sha256),
    CONSTRAINT customer_sessions_expiry_check CHECK (idle_expires_at <= expires_at AND expires_at > created_at)
);

CREATE INDEX customer_sessions_customer_idx ON customer_sessions (customer_id);
CREATE INDEX customer_sessions_purge_idx ON customer_sessions (expires_at);

-- Сессию нельзя «оживить» или переназначить: неизменны владелец, токен, эпоха, создание и абсолютный
-- срок; отозванная сессия не меняется совсем. Удаление (очистка worker) разрешено.
CREATE FUNCTION customer_sessions_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.revoked_at IS NOT NULL THEN
        RAISE EXCEPTION 'отозванная сессия не изменяется';
    END IF;
    IF NEW.customer_id <> OLD.customer_id OR NEW.token_sha256 <> OLD.token_sha256
       OR NEW.auth_epoch <> OLD.auth_epoch OR NEW.created_at <> OLD.created_at OR NEW.expires_at <> OLD.expires_at THEN
        RAISE EXCEPTION 'владелец, токен, эпоха и сроки сессии неизменяемы';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER customer_sessions_guard
    BEFORE UPDATE ON customer_sessions
    FOR EACH ROW EXECUTE FUNCTION customer_sessions_guard();

-- ---------- 4. заказы покупателя ----------

CREATE TABLE customer_orders (
    order_id INTEGER PRIMARY KEY REFERENCES orders(id),
    customer_id BIGINT NOT NULL REFERENCES customers(id),
    linked_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX customer_orders_customer_idx ON customer_orders (customer_id, order_id);

-- Принадлежность заказа не переписывается; удаление — только осознанно (уборка тестовых данных),
-- как у истории заказов 0002/0003.
CREATE FUNCTION customer_orders_protect() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' AND current_setting('digital_store.allow_history_delete', true) = 'on' THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION '% запрещён для customer_orders: привязка заказа неизменяема', TG_OP;
END
$$;

CREATE TRIGGER customer_orders_protect
    BEFORE UPDATE OR DELETE ON customer_orders
    FOR EACH ROW EXECUTE FUNCTION customer_orders_protect();

CREATE FUNCTION customer_orders_protect_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'TRUNCATE % запрещён', TG_TABLE_NAME;
END
$$;

CREATE TRIGGER customer_orders_protect_truncate BEFORE TRUNCATE ON customer_orders
    FOR EACH STATEMENT EXECUTE FUNCTION customer_orders_protect_truncate();

-- ---------- 5. ограничение попыток проверки пароля ----------
--
-- scope 1 — вход по email (ключ sha256('email:' || email_norm)), scope 2 — подтверждение пароля
-- покупателем при смене (ключ sha256('customer:' || id)). Фиксированное окно (REV5 §1).
-- pepper_missing_audited — запись auth.pepper_missing в аудит уже сделана в текущем окне.

CREATE TABLE auth_throttle (
    scope SMALLINT NOT NULL,
    key_sha256 BYTEA NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL,
    pepper_missing_audited BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (scope, key_sha256),
    CONSTRAINT auth_throttle_scope_check CHECK (scope IN (1, 2)),
    CONSTRAINT auth_throttle_key_sha256_check CHECK (octet_length(key_sha256) = 32),
    CONSTRAINT auth_throttle_attempts_check CHECK (attempts BETWEEN 0 AND 1000000)
);

CREATE INDEX auth_throttle_window_idx ON auth_throttle (window_start);

-- ---------- 6. внутренние проверки (SECURITY INVOKER, EXECUTE никому) ----------

CREATE FUNCTION auth_require_read_committed() RETURNS VOID
LANGUAGE plpgsql STABLE AS $$
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION 'функции аккаунтов работают только в READ COMMITTED (сейчас %)',
            current_setting('transaction_isolation');
    END IF;
END
$$;

CREATE FUNCTION auth_check_email(p_email_norm TEXT) RETURNS VOID
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF p_email_norm IS NULL OR length(p_email_norm) > 254 OR p_email_norm <> lower(p_email_norm)
       OR p_email_norm !~ '^[^@\s]+@[^@\s]+$' THEN
        RAISE EXCEPTION 'неверный формат email_norm';
    END IF;
END
$$;

-- Домен .invalid зарезервирован (RFC 2606) под служебные адреса released+<id>@invalid (освобождение email,
-- REV5 §3.2): регистрацией их не занять заранее, а войти по освобождённому адресу нельзя.
CREATE FUNCTION auth_check_not_reserved(p_email_norm TEXT) RETURNS VOID
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF p_email_norm LIKE '%@invalid' OR p_email_norm LIKE '%.invalid' THEN
        RAISE EXCEPTION 'адреса в домене invalid зарезервированы';
    END IF;
END
$$;

-- Доказательство пароля: base64url(HMAC-SHA256(перец_v, ...)) без '=' — ровно 43 символа (REV5 §5.1).
CREATE FUNCTION auth_check_proof(p_proof TEXT, p_ver SMALLINT) RETURNS VOID
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF p_proof IS NULL OR p_proof !~ '^[A-Za-z0-9_-]{43}$' THEN
        RAISE EXCEPTION 'неверный формат доказательства пароля';
    END IF;
    IF p_ver IS NULL OR p_ver < 1 THEN
        RAISE EXCEPTION 'неверная версия перца';
    END IF;
END
$$;

-- Текущее и (необязательно) предыдущее доказательство: prev — либо оба NULL, либо оба заданы и ver_prev < ver_cur.
CREATE FUNCTION auth_check_proof_pair(p_proof_cur TEXT, p_ver_cur SMALLINT, p_proof_prev TEXT, p_ver_prev SMALLINT)
RETURNS VOID
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    PERFORM auth_check_proof(p_proof_cur, p_ver_cur);
    IF p_proof_prev IS NULL AND p_ver_prev IS NULL THEN
        RETURN;
    END IF;
    PERFORM auth_check_proof(p_proof_prev, p_ver_prev);
    IF p_ver_prev >= p_ver_cur THEN
        RAISE EXCEPTION 'предыдущая версия перца должна быть меньше текущей';
    END IF;
END
$$;

CREATE FUNCTION auth_check_token(p_token_sha256 BYTEA) RETURNS VOID
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF p_token_sha256 IS NULL OR octet_length(p_token_sha256) <> 32 THEN
        RAISE EXCEPTION 'хэш токена сессии должен быть 32 байта';
    END IF;
END
$$;

-- Покупатель действительной сессии или NULL — условие REV5 §2.1 и флаг accounts_enabled ОДНИМ оператором
-- (одна точка линеаризации: флаг и сессия проверяются в одном снимке).
-- Модель времени: сроки сессии сравниваются с clock_timestamp() — моментом проверки, а не началом
-- транзакции (now()): после ожидания блокировки истёкшая за время ожидания сессия недействительна.
-- VOLATILE: результат зависит от момента вызова.
CREATE FUNCTION customer_session_customer(p_token_sha256 BYTEA) RETURNS BIGINT
LANGUAGE sql VOLATILE AS $$
    SELECT s.customer_id
    FROM customer_sessions s
    JOIN customers c ON c.id = s.customer_id
    WHERE s.token_sha256 = p_token_sha256
      AND s.revoked_at IS NULL AND s.expires_at > clock_timestamp() AND s.idle_expires_at > clock_timestamp()
      AND c.status = 'active' AND s.auth_epoch = c.auth_epoch
      AND EXISTS (SELECT 1 FROM auth_settings a WHERE a.singleton AND a.accounts_enabled)
$$;

-- Ответ при недействительной сессии: 'disabled', если аккаунты выключены на момент ответа, иначе
-- 'session_invalid'. Только уточняет причину; решение уже принято customer_session_customer.
CREATE FUNCTION auth_session_refusal() RETURNS JSONB
LANGUAGE sql VOLATILE AS $$
    SELECT jsonb_build_object('result',
        CASE WHEN EXISTS (SELECT 1 FROM auth_settings WHERE singleton AND accounts_enabled)
             THEN 'session_invalid' ELSE 'disabled' END)
$$;

-- Повысить эпоху и отозвать все сессии покупателя. Вызывающий УЖЕ держит блокировки (2) и (3).
CREATE FUNCTION customer_sessions_invalidate(p_customer_id BIGINT) RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE customers SET auth_epoch = auth_epoch + 1 WHERE id = p_customer_id;
    UPDATE customer_sessions SET revoked_at = now() WHERE customer_id = p_customer_id AND revoked_at IS NULL;
END
$$;

-- ---------- 7. функции веб-приложения (SECURITY DEFINER) ----------

-- Регистрация (REV4 §4, ADDENDUM2 §1.2): счётчика в базе нет (лимиты — в приложении). Сначала bcrypt
-- нового доказательства (хэширование, ничего не раскрывает), затем вставка с ожиданием не дольше lock_timeout.
-- Результат: created | email_taken | busy | disabled | closed.
CREATE FUNCTION auth_register(p_email_norm TEXT, p_proof TEXT, p_ver SMALLINT, p_new_session BYTEA)
RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET lock_timeout = '2s'
AS $$
DECLARE
    v_settings auth_settings%ROWTYPE;
    v_hash TEXT;
    v_id BIGINT;
    v_epoch BIGINT;
BEGIN
    PERFORM auth_require_read_committed();
    PERFORM auth_check_email(p_email_norm);
    PERFORM auth_check_not_reserved(p_email_norm);
    PERFORM auth_check_proof(p_proof, p_ver);
    PERFORM auth_check_token(p_new_session);

    SELECT * INTO v_settings FROM auth_settings WHERE singleton;
    IF NOT FOUND OR NOT v_settings.accounts_enabled THEN
        RETURN jsonb_build_object('result', 'disabled');
    END IF;
    IF NOT v_settings.registration_open THEN
        RETURN jsonb_build_object('result', 'closed');
    END IF;

    v_hash := ext_pgcrypto.crypt(p_proof, ext_pgcrypto.gen_salt('bf', v_settings.bcrypt_cost::INTEGER));

    BEGIN
        INSERT INTO customers (email_norm, password_hash, pepper_version)
        VALUES (p_email_norm, v_hash, p_ver)
        ON CONFLICT (email_norm) DO NOTHING
        RETURNING id, auth_epoch INTO v_id, v_epoch;
    EXCEPTION WHEN lock_not_available THEN
        RETURN jsonb_build_object('result', 'busy');
    END;
    IF v_id IS NULL THEN
        RETURN jsonb_build_object('result', 'email_taken');
    END IF;

    INSERT INTO customer_sessions (customer_id, token_sha256, auth_epoch, idle_expires_at, expires_at)
    VALUES (v_id, p_new_session, v_epoch, now() + v_settings.session_idle, now() + v_settings.session_absolute);
    INSERT INTO audit_log (actor, action, details)
    VALUES ('customer', 'auth.register', jsonb_build_object('customer_id', v_id));
    RETURN jsonb_build_object('result', 'created');
END
$$;

-- Вход (REV4 §3, REV5 §1, §2, §5.1, ADDENDUM §1.2, ADDENDUM2 §1.2).
-- Результат: ok | invalid | throttled (+ retry_after_seconds) | busy | disabled.
-- Неизвестный email, неверный пароль, отключённый покупатель и отсутствие нужной версии перца
-- дают одинаковый 'invalid' после bcrypt той же стоимости.
CREATE FUNCTION auth_login(
    p_email_norm TEXT, p_proof_cur TEXT, p_ver_cur SMALLINT, p_proof_prev TEXT, p_ver_prev SMALLINT,
    p_new_session BYTEA, p_prev_session BYTEA
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET lock_timeout = '2s'
AS $$
DECLARE
    v_settings auth_settings%ROWTYPE;
    v_key BYTEA;
    v_attempts INTEGER;
    v_window_start TIMESTAMPTZ;
    v_pepper_audited BOOLEAN;
    v_customer customers%ROWTYPE;
    v_found BOOLEAN;
    v_proof TEXT;
    v_hash TEXT;
    v_matched BOOLEAN := FALSE;
    v_ok BOOLEAN;
    v_rehashed BOOLEAN := FALSE;
BEGIN
    PERFORM auth_require_read_committed();
    PERFORM auth_check_email(p_email_norm);
    PERFORM auth_check_not_reserved(p_email_norm);
    PERFORM auth_check_proof_pair(p_proof_cur, p_ver_cur, p_proof_prev, p_ver_prev);
    PERFORM auth_check_token(p_new_session);
    IF p_prev_session IS NOT NULL THEN
        PERFORM auth_check_token(p_prev_session);
    END IF;

    SELECT * INTO v_settings FROM auth_settings WHERE singleton;
    IF NOT FOUND OR NOT v_settings.accounts_enabled THEN
        RETURN jsonb_build_object('result', 'disabled');
    END IF;
    v_key := pg_catalog.sha256(pg_catalog.convert_to('email:' || p_email_norm, 'UTF8'));

    -- Этап блокировок: (1) счётчик, (2) покупатель, (3) прежняя сессия этого покупателя. Ожидание дольше
    -- lock_timeout → 'busy'; подтранзакция откатывает и инкремент — bcrypt ещё не выполнялся.
    BEGIN
        INSERT INTO auth_throttle AS t (scope, key_sha256, window_start, attempts)
        VALUES (1, v_key, now(), 1)
        ON CONFLICT (scope, key_sha256) DO UPDATE SET
            window_start = CASE WHEN t.window_start <= now() - v_settings.login_window THEN now() ELSE t.window_start END,
            attempts = CASE WHEN t.window_start <= now() - v_settings.login_window THEN 1
                            ELSE least(t.attempts + 1, 1000000) END,
            pepper_missing_audited = CASE WHEN t.window_start <= now() - v_settings.login_window THEN FALSE
                                          ELSE t.pepper_missing_audited END
        RETURNING attempts, window_start, pepper_missing_audited INTO v_attempts, v_window_start, v_pepper_audited;

        SELECT * INTO v_customer FROM customers WHERE email_norm = p_email_norm FOR NO KEY UPDATE;
        v_found := FOUND;

        IF v_found AND p_prev_session IS NOT NULL THEN
            PERFORM 1 FROM customer_sessions
            WHERE token_sha256 = p_prev_session AND customer_id = v_customer.id AND revoked_at IS NULL
            FOR NO KEY UPDATE;
        END IF;
    EXCEPTION WHEN lock_not_available THEN
        RETURN jsonb_build_object('result', 'busy');
    END;

    IF v_attempts > v_settings.login_limit THEN
        -- Ровно одна запись аудита на покупателя за окно — при переходе в блокировку. Неизвестный email
        -- записей не порождает.
        IF v_found AND v_attempts = v_settings.login_limit + 1 THEN
            INSERT INTO audit_log (actor, action, details)
            VALUES ('customer', 'auth.login_throttled', jsonb_build_object('customer_id', v_customer.id));
        END IF;
        RETURN jsonb_build_object('result', 'throttled', 'retry_after_seconds',
            greatest(1, ceil(extract(epoch FROM v_window_start + v_settings.login_window - now())))::INTEGER);
    END IF;

    -- Выбор доказательства по версии перца, записанной у покупателя. Нет покупателя или нужной версии —
    -- bcrypt по фиктивному хэшу той же стоимости (одинаковое время), результат 'invalid'.
    v_hash := v_settings.dummy_hash;
    v_proof := p_proof_cur;
    IF v_found THEN
        IF v_customer.pepper_version = p_ver_cur THEN
            v_hash := v_customer.password_hash;
            v_matched := TRUE;
        ELSIF p_ver_prev IS NOT NULL AND v_customer.pepper_version = p_ver_prev THEN
            v_hash := v_customer.password_hash;
            v_proof := p_proof_prev;
            v_matched := TRUE;
        END IF;
    END IF;
    v_ok := ext_pgcrypto.crypt(v_proof, v_hash) = v_hash;

    IF v_found AND NOT v_matched AND NOT v_pepper_audited THEN
        -- Нужной версии перца нет среди переданных: одна запись аудита на покупателя за окно.
        UPDATE auth_throttle SET pepper_missing_audited = TRUE WHERE scope = 1 AND key_sha256 = v_key;
        INSERT INTO audit_log (actor, action, details)
        VALUES ('customer', 'auth.pepper_missing',
                jsonb_build_object('customer_id', v_customer.id, 'pepper_version', v_customer.pepper_version));
    END IF;

    IF NOT v_found OR NOT v_matched OR NOT v_ok OR v_customer.status <> 'active' THEN
        RETURN jsonb_build_object('result', 'invalid');
    END IF;

    -- Успех: новая сессия с эпохой из заблокированной строки; прежняя сессия этого же покупателя
    -- отзывается (защита от фиксации); счётчик сбрасывается; при старой версии перца — пересчёт хэша
    -- без изменения auth_epoch (ADDENDUM §1.2).
    INSERT INTO customer_sessions (customer_id, token_sha256, auth_epoch, idle_expires_at, expires_at)
    VALUES (v_customer.id, p_new_session, v_customer.auth_epoch,
            now() + v_settings.session_idle, now() + v_settings.session_absolute);
    IF p_prev_session IS NOT NULL THEN
        UPDATE customer_sessions SET revoked_at = now()
        WHERE token_sha256 = p_prev_session AND customer_id = v_customer.id AND revoked_at IS NULL;
    END IF;
    UPDATE auth_throttle SET attempts = 0 WHERE scope = 1 AND key_sha256 = v_key;
    IF v_customer.pepper_version < p_ver_cur THEN
        UPDATE customers
        SET password_hash = ext_pgcrypto.crypt(p_proof_cur, ext_pgcrypto.gen_salt('bf', v_settings.bcrypt_cost::INTEGER)),
            pepper_version = p_ver_cur
        WHERE id = v_customer.id;
        v_rehashed := TRUE;
    END IF;
    INSERT INTO audit_log (actor, action, details)
    VALUES ('customer', 'auth.login', jsonb_build_object('customer_id', v_customer.id, 'rehashed', v_rehashed));
    RETURN jsonb_build_object('result', 'ok');
END
$$;

-- Смена пароля (REV5 §2.1, ADDENDUM2 §1.2). Новое доказательство — одно, текущей версии перца.
-- Результат: ok | invalid | session_invalid | throttled (+ retry_after_seconds) | busy | disabled.
CREATE FUNCTION auth_password_change(
    p_session BYTEA, p_proof_cur TEXT, p_ver_cur SMALLINT, p_proof_prev TEXT, p_ver_prev SMALLINT,
    p_new_proof TEXT, p_new_ver SMALLINT, p_new_session BYTEA
) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET lock_timeout = '2s'
AS $$
DECLARE
    v_settings auth_settings%ROWTYPE;
    v_cid BIGINT;
    v_key BYTEA;
    v_attempts INTEGER;
    v_window_start TIMESTAMPTZ;
    v_pepper_audited BOOLEAN;
    v_customer customers%ROWTYPE;
    v_proof TEXT;
    v_hash TEXT;
    v_matched BOOLEAN := FALSE;
    v_ok BOOLEAN;
BEGIN
    PERFORM auth_require_read_committed();
    PERFORM auth_check_token(p_session);
    PERFORM auth_check_proof_pair(p_proof_cur, p_ver_cur, p_proof_prev, p_ver_prev);
    PERFORM auth_check_proof(p_new_proof, p_new_ver);
    IF p_new_ver <> p_ver_cur THEN
        RAISE EXCEPTION 'новое доказательство должно быть текущей версии перца';
    END IF;
    PERFORM auth_check_token(p_new_session);
    IF p_new_session = p_session THEN
        RAISE EXCEPTION 'новая сессия должна отличаться от текущей';
    END IF;

    SELECT * INTO v_settings FROM auth_settings WHERE singleton;
    IF NOT FOUND OR NOT v_settings.accounts_enabled THEN
        RETURN jsonb_build_object('result', 'disabled');
    END IF;

    -- Покупатель сессии (только id — он у сессии неизменен); действительность — после блокировок.
    SELECT customer_id INTO v_cid FROM customer_sessions WHERE token_sha256 = p_session;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('result', 'session_invalid');
    END IF;
    v_key := pg_catalog.sha256(pg_catalog.convert_to('customer:' || v_cid::TEXT, 'UTF8'));

    BEGIN
        INSERT INTO auth_throttle AS t (scope, key_sha256, window_start, attempts)
        VALUES (2, v_key, now(), 1)
        ON CONFLICT (scope, key_sha256) DO UPDATE SET
            window_start = CASE WHEN t.window_start <= now() - v_settings.login_window THEN now() ELSE t.window_start END,
            attempts = CASE WHEN t.window_start <= now() - v_settings.login_window THEN 1
                            ELSE least(t.attempts + 1, 1000000) END,
            pepper_missing_audited = CASE WHEN t.window_start <= now() - v_settings.login_window THEN FALSE
                                          ELSE t.pepper_missing_audited END
        RETURNING attempts, window_start, pepper_missing_audited INTO v_attempts, v_window_start, v_pepper_audited;

        SELECT * INTO v_customer FROM customers WHERE id = v_cid FOR NO KEY UPDATE;
        PERFORM 1 FROM customer_sessions WHERE customer_id = v_cid AND revoked_at IS NULL FOR NO KEY UPDATE;
    EXCEPTION WHEN lock_not_available THEN
        RETURN jsonb_build_object('result', 'busy');
    END;

    -- Отдельный оператор после блокировок: видит всё, что зафиксировали транзакции, которых ждали
    -- (эпоху, отзыв, отключение, флаг аккаунтов), и сроки на момент проверки. Это точка линеаризации.
    IF customer_session_customer(p_session) IS DISTINCT FROM v_cid THEN
        RETURN auth_session_refusal();
    END IF;

    IF v_attempts > v_settings.login_limit THEN
        IF v_attempts = v_settings.login_limit + 1 THEN
            INSERT INTO audit_log (actor, action, details)
            VALUES ('customer', 'auth.password_throttled', jsonb_build_object('customer_id', v_cid));
        END IF;
        RETURN jsonb_build_object('result', 'throttled', 'retry_after_seconds',
            greatest(1, ceil(extract(epoch FROM v_window_start + v_settings.login_window - now())))::INTEGER);
    END IF;

    v_hash := v_settings.dummy_hash;
    v_proof := p_proof_cur;
    IF v_customer.pepper_version = p_ver_cur THEN
        v_hash := v_customer.password_hash;
        v_matched := TRUE;
    ELSIF p_ver_prev IS NOT NULL AND v_customer.pepper_version = p_ver_prev THEN
        v_hash := v_customer.password_hash;
        v_proof := p_proof_prev;
        v_matched := TRUE;
    END IF;
    v_ok := ext_pgcrypto.crypt(v_proof, v_hash) = v_hash;

    IF NOT v_matched THEN
        IF NOT v_pepper_audited THEN
            UPDATE auth_throttle SET pepper_missing_audited = TRUE WHERE scope = 2 AND key_sha256 = v_key;
            INSERT INTO audit_log (actor, action, details)
            VALUES ('customer', 'auth.pepper_missing',
                    jsonb_build_object('customer_id', v_cid, 'pepper_version', v_customer.pepper_version));
        END IF;
        RETURN jsonb_build_object('result', 'invalid');
    END IF;
    IF NOT v_ok THEN
        RETURN jsonb_build_object('result', 'invalid');
    END IF;

    UPDATE customers
    SET password_hash = ext_pgcrypto.crypt(p_new_proof, ext_pgcrypto.gen_salt('bf', v_settings.bcrypt_cost::INTEGER)),
        pepper_version = p_new_ver, password_changed_at = now()
    WHERE id = v_cid;
    PERFORM customer_sessions_invalidate(v_cid);
    INSERT INTO customer_sessions (customer_id, token_sha256, auth_epoch, idle_expires_at, expires_at)
    VALUES (v_cid, p_new_session, v_customer.auth_epoch + 1,
            now() + v_settings.session_idle, now() + v_settings.session_absolute);
    UPDATE auth_throttle SET attempts = 0 WHERE scope = 2 AND key_sha256 = v_key;
    INSERT INTO audit_log (actor, action, details)
    VALUES ('customer', 'auth.password_change', jsonb_build_object('customer_id', v_cid));
    RETURN jsonb_build_object('result', 'ok');
END
$$;

-- Действительна ли сессия. Только чтение, без блокировок (ADDENDUM §1.2). Флаг и сессия — в одном
-- операторе customer_session_customer. Результат: {"valid": bool}.
CREATE FUNCTION auth_session_resolve(p_session BYTEA) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    PERFORM auth_check_token(p_session);
    RETURN jsonb_build_object('valid', customer_session_customer(p_session) IS NOT NULL);
END
$$;

-- Продление простоя (решение Q5). Отдельная транзакция после успешного resolve. Блокирует только строку
-- сессии и только с SKIP LOCKED: занятую строку (например, её отзывает смена пароля) пропускает.
-- После блокировки строки действительность проверяется заново (customer_session_customer — свежий снимок,
-- флаг, эпоха, статус, сроки на момент проверки) и по самой заблокированной версии (revoked_at).
-- revoked_at не очищается, idle_expires_at не уменьшается и не превышает абсолютный срок (триггер
-- запрещает менять отозванную сессию и абсолютный срок). Результат: {"touched": bool}.
CREATE FUNCTION auth_session_touch(p_session BYTEA) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_settings auth_settings%ROWTYPE;
    v_count INTEGER;
BEGIN
    PERFORM auth_check_token(p_session);
    SELECT * INTO v_settings FROM auth_settings WHERE singleton;
    IF NOT FOUND OR NOT v_settings.accounts_enabled THEN
        RETURN jsonb_build_object('touched', FALSE);
    END IF;

    UPDATE customer_sessions s
    SET last_seen_at = clock_timestamp(),
        idle_expires_at = least(greatest(s.idle_expires_at, clock_timestamp() + v_settings.session_idle), s.expires_at)
    WHERE s.id = (
            SELECT s2.id FROM customer_sessions s2
            WHERE s2.token_sha256 = p_session
              AND s2.last_seen_at < clock_timestamp() - v_settings.session_touch_interval
              AND s2.revoked_at IS NULL
            FOR NO KEY UPDATE SKIP LOCKED)
      AND s.revoked_at IS NULL
      AND customer_session_customer(p_session) = s.customer_id;
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN jsonb_build_object('touched', v_count > 0);
END
$$;

-- Выход: отозвать эту сессию. Идемпотентно; работает и при выключенных аккаунтах.
CREATE FUNCTION auth_session_revoke(p_session BYTEA) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    PERFORM auth_check_token(p_session);
    UPDATE customer_sessions SET revoked_at = now() WHERE token_sha256 = p_session AND revoked_at IS NULL;
    RETURN jsonb_build_object('result', 'ok');
END
$$;

-- «Выйти на всех устройствах»: эпоха + 1, отзыв всех сессий, включая текущую.
-- Результат: ok | session_invalid | busy | disabled.
CREATE FUNCTION auth_sessions_revoke_all(p_session BYTEA) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER
SET lock_timeout = '2s'
AS $$
DECLARE
    v_cid BIGINT;
BEGIN
    PERFORM auth_require_read_committed();
    PERFORM auth_check_token(p_session);
    IF NOT EXISTS (SELECT 1 FROM auth_settings WHERE singleton AND accounts_enabled) THEN
        RETURN jsonb_build_object('result', 'disabled');
    END IF;
    SELECT customer_id INTO v_cid FROM customer_sessions WHERE token_sha256 = p_session;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('result', 'session_invalid');
    END IF;
    BEGIN
        PERFORM 1 FROM customers WHERE id = v_cid FOR NO KEY UPDATE;
        PERFORM 1 FROM customer_sessions WHERE customer_id = v_cid AND revoked_at IS NULL FOR NO KEY UPDATE;
    EXCEPTION WHEN lock_not_available THEN
        RETURN jsonb_build_object('result', 'busy');
    END;
    -- Точка линеаризации — проверка после блокировок (как в смене пароля).
    IF customer_session_customer(p_session) IS DISTINCT FROM v_cid THEN
        RETURN auth_session_refusal();
    END IF;
    PERFORM customer_sessions_invalidate(v_cid);
    INSERT INTO audit_log (actor, action, details)
    VALUES ('customer', 'auth.sessions_revoke_all', jsonb_build_object('customer_id', v_cid));
    RETURN jsonb_build_object('result', 'ok');
END
$$;

-- Профиль: email и дата создания. NULL — сессия недействительна или аккаунты выключены.
CREATE FUNCTION customer_profile_view(p_session BYTEA) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_cid BIGINT;
BEGIN
    PERFORM auth_check_token(p_session);
    v_cid := customer_session_customer(p_session);       -- флаг и сессия — одним оператором
    IF v_cid IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN (SELECT jsonb_build_object('email', c.email_norm, 'created_at', c.created_at)
            FROM customers c WHERE c.id = v_cid);
END
$$;

-- Заказы покупателя: только привязанные к нему (customer_orders). Состав, сумма, статус и даты —
-- без access_token и без платёжных данных. NULL — сессия недействительна или аккаунты выключены.
CREATE FUNCTION customer_orders_view(p_session BYTEA) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_cid BIGINT;
BEGIN
    PERFORM auth_check_token(p_session);
    v_cid := customer_session_customer(p_session);       -- флаг и сессия — одним оператором
    IF v_cid IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN coalesce((
        SELECT jsonb_agg(jsonb_build_object('id', o.id, 'status', o.status, 'created_at', o.created_at,
                                            'total_minor', o.total_minor, 'currency', o.currency)
                         ORDER BY o.id DESC)
        FROM customer_orders co JOIN orders o ON o.id = co.order_id
        WHERE co.customer_id = v_cid), '[]'::JSONB);
END
$$;

-- Один заказ покупателя с составом. Чужой, несуществующий заказ и недействительная сессия — одинаковый NULL.
CREATE FUNCTION customer_order_view(p_session BYTEA, p_order_id INTEGER) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_cid BIGINT;
BEGIN
    PERFORM auth_check_token(p_session);
    v_cid := customer_session_customer(p_session);       -- флаг и сессия — одним оператором
    IF v_cid IS NULL OR p_order_id IS NULL THEN
        RETURN NULL;
    END IF;
    RETURN (
        SELECT jsonb_build_object(
            'order', jsonb_build_object('id', o.id, 'status', o.status, 'created_at', o.created_at,
                                        'total_minor', o.total_minor, 'currency', o.currency),
            'items', coalesce((
                SELECT jsonb_agg(jsonb_build_object(
                           'product_id', i.product_id, 'name', i.product_name, 'product_type', i.product_type,
                           'quantity', i.quantity, 'unit_price_minor', i.unit_price_minor,
                           'line_total_minor', i.line_total_minor) ORDER BY i.id)
                FROM order_items i WHERE i.order_id = o.id), '[]'::JSONB))
        FROM customer_orders co JOIN orders o ON o.id = co.order_id
        WHERE co.order_id = p_order_id AND co.customer_id = v_cid);
END
$$;

-- Привязка НОВОГО заказа к покупателю — в транзакции создания заказа, после выдачи access_token
-- (ADDENDUM2 §3.2). (a) id покупателя по токену; (b) FOR SHARE на строке покупателя — ждёт смену эпохи,
-- отключение, освобождение email; (c) ОТДЕЛЬНЫМ оператором полная проверка сессии (новый снимок);
-- (d) заказ виден, pending и не запечатан, то есть создан в этой транзакции (как order_access_issue, PR3).
-- Результат: linked | session_invalid | disabled — при любом отказе вызывающий откатывает всю транзакцию
-- заказа. Неподходящий заказ (чужой, запечатанный, повторная привязка) — RAISE. lock_timeout не задаётся.
CREATE FUNCTION customer_order_link_new(p_session BYTEA, p_order_id INTEGER) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_cid BIGINT;
    v_valid_cid BIGINT;
    v_status TEXT;
    v_sealed BOOLEAN;
BEGIN
    PERFORM auth_require_read_committed();
    PERFORM auth_check_token(p_session);
    IF p_order_id IS NULL THEN
        RAISE EXCEPTION 'не указан заказ';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM auth_settings WHERE singleton AND accounts_enabled) THEN
        RETURN jsonb_build_object('result', 'disabled');
    END IF;

    -- (a)
    SELECT customer_id INTO v_cid FROM customer_sessions WHERE token_sha256 = p_session;
    IF NOT FOUND THEN
        RETURN jsonb_build_object('result', 'session_invalid');
    END IF;

    -- (b)
    PERFORM 1 FROM customers WHERE id = v_cid FOR SHARE;

    -- (c) новый оператор → новый снимок: видит то, что зафиксировала транзакция, которую ждали на (b),
    -- и флаг аккаунтов на тот же момент; сроки — на момент проверки (clock_timestamp), а не начала
    -- транзакции заказа. Это точка линеаризации привязки: флаг, сессия и покупатель — в одном снимке.
    SELECT s.customer_id INTO v_valid_cid
    FROM customer_sessions s JOIN customers c ON c.id = s.customer_id
    WHERE s.token_sha256 = p_session AND s.revoked_at IS NULL
      AND s.expires_at > clock_timestamp() AND s.idle_expires_at > clock_timestamp()
      AND c.status = 'active' AND s.auth_epoch = c.auth_epoch
      AND EXISTS (SELECT 1 FROM auth_settings a WHERE a.singleton AND a.accounts_enabled);
    IF v_valid_cid IS DISTINCT FROM v_cid THEN
        RETURN auth_session_refusal();
    END IF;

    -- (d) Заказу уже выдан гостевой access_token (PR3): он остаётся единственным доступом к оплате,
    -- статусу и выдаче ключей и для привязанного заказа (REV4 §6.4).
    SELECT status, items_sealed INTO v_status, v_sealed FROM orders WHERE id = p_order_id;
    IF NOT FOUND OR v_sealed OR v_status <> 'pending' THEN
        RAISE EXCEPTION 'привязывается только новый заказ в транзакции его создания';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM order_access WHERE order_id = p_order_id) THEN
        RAISE EXCEPTION 'сначала выдаётся секрет доступа заказа (order_access_issue), затем привязка';
    END IF;

    INSERT INTO customer_orders (order_id, customer_id) VALUES (p_order_id, v_cid);
    RETURN jsonb_build_object('result', 'linked');
END
$$;

-- ---------- 8. обслуживание (worker) ----------

-- Удаляет счётчики с истёкшим окном и давно недействительные сессии. Занятые строки пропускает.
CREATE FUNCTION auth_maintenance_purge() RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
    v_window INTERVAL;
    v_throttle INTEGER;
    v_sessions INTEGER;
BEGIN
    SELECT login_window INTO v_window FROM auth_settings WHERE singleton;
    v_window := coalesce(v_window, INTERVAL '15 minutes');

    WITH doomed AS (
        SELECT scope, key_sha256 FROM auth_throttle
        WHERE window_start < now() - v_window
        LIMIT 10000
        FOR UPDATE SKIP LOCKED)
    DELETE FROM auth_throttle t USING doomed d WHERE t.scope = d.scope AND t.key_sha256 = d.key_sha256;
    GET DIAGNOSTICS v_throttle = ROW_COUNT;

    WITH doomed AS (
        SELECT id FROM customer_sessions
        WHERE expires_at < now() - INTERVAL '1 day' OR idle_expires_at < now() - INTERVAL '1 day'
           OR revoked_at < now() - INTERVAL '1 day'
        LIMIT 10000
        FOR UPDATE SKIP LOCKED)
    DELETE FROM customer_sessions s USING doomed d WHERE s.id = d.id;
    GET DIAGNOSTICS v_sessions = ROW_COUNT;

    RETURN jsonb_build_object('throttle_deleted', v_throttle, 'sessions_deleted', v_sessions);
END
$$;

-- ---------- 9. администрирование ----------
--
-- Как в PR5: ядро *_core — SECURITY INVOKER, EXECUTE не выдаётся никому (вызывают обёртки и владелец
-- в тестах); обёртка admin_* — SECURITY DEFINER, проверяет администратора через shop_require_admin (0004).
-- В аудит — только customer_id, причина и кто выполнил; email не пишется.

CREATE FUNCTION auth_check_reason(p_reason TEXT) RETURNS VOID
LANGUAGE plpgsql IMMUTABLE AS $$
BEGIN
    IF p_reason IS NULL OR char_length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'укажите причину';
    END IF;
END
$$;

-- Найти покупателя по email (решение Q7): минимум данных, каждый вызов — в аудит.
CREATE FUNCTION customer_lookup_core(p_email_norm TEXT, p_actor TEXT) RETURNS JSONB
LANGUAGE plpgsql AS $$
DECLARE
    v_customer RECORD;
BEGIN
    PERFORM auth_check_email(p_email_norm);
    SELECT id, status, created_at INTO v_customer FROM customers WHERE email_norm = p_email_norm;
    INSERT INTO audit_log (actor, action, details)
    VALUES ('admin', 'admin.customer_lookup',
            jsonb_build_object('by', p_actor, 'found', v_customer.id IS NOT NULL, 'customer_id', v_customer.id));
    IF v_customer.id IS NULL THEN
        RETURN jsonb_build_object('result', 'not_found');
    END IF;
    RETURN jsonb_build_object('result', 'found', 'customer_id', v_customer.id, 'status', v_customer.status,
                              'created_at', v_customer.created_at);
END
$$;

-- Отключить покупателя: status = 'disabled', эпоха + 1, все сессии отозваны. Результат: ok | not_found | busy.
CREATE FUNCTION customer_disable_core(p_customer_id BIGINT, p_reason TEXT, p_actor TEXT) RETURNS JSONB
LANGUAGE plpgsql
SET lock_timeout = '2s'
AS $$
DECLARE
    v_found BOOLEAN;
BEGIN
    PERFORM auth_check_reason(p_reason);
    BEGIN
        PERFORM 1 FROM customers WHERE id = p_customer_id FOR NO KEY UPDATE;
        v_found := FOUND;
        PERFORM 1 FROM customer_sessions WHERE customer_id = p_customer_id AND revoked_at IS NULL FOR NO KEY UPDATE;
    EXCEPTION WHEN lock_not_available THEN
        RETURN jsonb_build_object('result', 'busy');
    END;
    IF NOT v_found THEN
        RETURN jsonb_build_object('result', 'not_found');
    END IF;
    UPDATE customers SET status = 'disabled', status_changed_at = now() WHERE id = p_customer_id;
    PERFORM customer_sessions_invalidate(p_customer_id);
    INSERT INTO audit_log (actor, action, details)
    VALUES ('admin', 'admin.customer_disable',
            jsonb_build_object('customer_id', p_customer_id, 'by', p_actor, 'reason', left(p_reason, 200)));
    RETURN jsonb_build_object('result', 'ok');
END
$$;

-- Отозвать все сессии покупателя (решение Q6): эпоха + 1, статус не меняется. Результат: ok | not_found | busy.
CREATE FUNCTION customer_revoke_sessions_core(p_customer_id BIGINT, p_reason TEXT, p_actor TEXT) RETURNS JSONB
LANGUAGE plpgsql
SET lock_timeout = '2s'
AS $$
DECLARE
    v_found BOOLEAN;
BEGIN
    PERFORM auth_check_reason(p_reason);
    BEGIN
        PERFORM 1 FROM customers WHERE id = p_customer_id FOR NO KEY UPDATE;
        v_found := FOUND;
        PERFORM 1 FROM customer_sessions WHERE customer_id = p_customer_id AND revoked_at IS NULL FOR NO KEY UPDATE;
    EXCEPTION WHEN lock_not_available THEN
        RETURN jsonb_build_object('result', 'busy');
    END;
    IF NOT v_found THEN
        RETURN jsonb_build_object('result', 'not_found');
    END IF;
    PERFORM customer_sessions_invalidate(p_customer_id);
    INSERT INTO audit_log (actor, action, details)
    VALUES ('admin', 'admin.customer_revoke_sessions',
            jsonb_build_object('customer_id', p_customer_id, 'by', p_actor, 'reason', left(p_reason, 200)));
    RETURN jsonb_build_object('result', 'ok');
END
$$;

-- Освободить email (REV5 §3.2): email → released+<id>@invalid, эпоха + 1, сессии отозваны. Заказы
-- остаются за аккаунтом, гостевые access_token работают. FOR UPDATE — меняется уникальный email_norm.
-- Результат: ok | already_released | not_found | busy.
CREATE FUNCTION customer_release_email_core(p_customer_id BIGINT, p_reason TEXT, p_actor TEXT) RETURNS JSONB
LANGUAGE plpgsql
SET lock_timeout = '2s'
AS $$
DECLARE
    v_email TEXT;
    v_found BOOLEAN;
BEGIN
    PERFORM auth_check_reason(p_reason);
    BEGIN
        SELECT email_norm INTO v_email FROM customers WHERE id = p_customer_id FOR UPDATE;
        v_found := FOUND;
        PERFORM 1 FROM customer_sessions WHERE customer_id = p_customer_id AND revoked_at IS NULL FOR NO KEY UPDATE;
    EXCEPTION WHEN lock_not_available THEN
        RETURN jsonb_build_object('result', 'busy');
    END;
    IF NOT v_found THEN
        RETURN jsonb_build_object('result', 'not_found');
    END IF;
    IF v_email = 'released+' || p_customer_id::TEXT || '@invalid' THEN
        RETURN jsonb_build_object('result', 'already_released');
    END IF;
    UPDATE customers SET email_norm = 'released+' || p_customer_id::TEXT || '@invalid' WHERE id = p_customer_id;
    PERFORM customer_sessions_invalidate(p_customer_id);
    INSERT INTO audit_log (actor, action, details)
    VALUES ('admin', 'admin.customer_release_email',
            jsonb_build_object('customer_id', p_customer_id, 'by', p_actor, 'reason', left(p_reason, 200)));
    RETURN jsonb_build_object('result', 'ok');
END
$$;

-- Изменить флаги аккаунтов. Остальные параметры auth_settings меняет только владелец схемы.
CREATE FUNCTION auth_settings_update_core(p_accounts_enabled BOOLEAN, p_registration_open BOOLEAN, p_actor TEXT)
RETURNS JSONB
LANGUAGE plpgsql AS $$
BEGIN
    IF p_accounts_enabled IS NULL OR p_registration_open IS NULL THEN
        RAISE EXCEPTION 'флаги должны быть заданы явно';
    END IF;
    UPDATE auth_settings SET accounts_enabled = p_accounts_enabled, registration_open = p_registration_open
    WHERE singleton;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'нет строки auth_settings';
    END IF;
    INSERT INTO audit_log (actor, action, details)
    VALUES ('admin', 'admin.auth_settings_update',
            jsonb_build_object('by', p_actor, 'accounts_enabled', p_accounts_enabled,
                               'registration_open', p_registration_open));
    RETURN jsonb_build_object('result', 'ok', 'accounts_enabled', p_accounts_enabled,
                              'registration_open', p_registration_open);
END
$$;

CREATE FUNCTION admin_customer_lookup(p_email_norm TEXT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN customer_lookup_core(p_email_norm, shop_require_admin('admin_customer_lookup(text)'::regprocedure));
END
$$;

CREATE FUNCTION admin_customer_disable(p_customer_id BIGINT, p_reason TEXT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN customer_disable_core(p_customer_id, p_reason,
        shop_require_admin('admin_customer_disable(bigint, text)'::regprocedure));
END
$$;

CREATE FUNCTION admin_customer_revoke_sessions(p_customer_id BIGINT, p_reason TEXT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN customer_revoke_sessions_core(p_customer_id, p_reason,
        shop_require_admin('admin_customer_revoke_sessions(bigint, text)'::regprocedure));
END
$$;

CREATE FUNCTION admin_customer_release_email(p_customer_id BIGINT, p_reason TEXT) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN customer_release_email_core(p_customer_id, p_reason,
        shop_require_admin('admin_customer_release_email(bigint, text)'::regprocedure));
END
$$;

CREATE FUNCTION admin_auth_settings_update(p_accounts_enabled BOOLEAN, p_registration_open BOOLEAN) RETURNS JSONB
LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
    RETURN auth_settings_update_core(p_accounts_enabled, p_registration_open,
        shop_require_admin('admin_auth_settings_update(boolean, boolean)'::regprocedure));
END
$$;

-- ---------- 10. начальные настройки: аккаунты и регистрация ВЫКЛЮЧЕНЫ ----------

-- Фиктивный хэш (стоимость 12) вычисляет триггер auth_settings_dummy_hash.
INSERT INTO auth_settings DEFAULT VALUES;

-- ---------- 11. доверенный search_path и права на функции ----------
--
-- Как в 0003/0004: путь pg_catalog, <схема магазина>, pg_temp (pg_temp последним — временные объекты
-- не подменят ни таблицы, ни функции). EXECUTE у PUBLIC отозван у всех функций 0005; права ролям
-- выдаёт deploy/db_roles.py.

DO $$
DECLARE
    shop_schema TEXT := current_schema();
    fn REGPROCEDURE;
    expected CONSTANT TEXT[] := ARRAY[
        'auth_settings_dummy_hash', 'customer_sessions_guard', 'customer_orders_protect',
        'customer_orders_protect_truncate', 'auth_require_read_committed', 'auth_check_email',
        'auth_check_not_reserved', 'auth_check_proof',
        'auth_check_proof_pair', 'auth_check_token', 'customer_session_customer', 'customer_sessions_invalidate',
        'auth_session_refusal', 'customers_hash_cost_guard',
        'auth_register', 'auth_login', 'auth_password_change', 'auth_session_resolve', 'auth_session_touch',
        'auth_session_revoke', 'auth_sessions_revoke_all', 'customer_profile_view', 'customer_orders_view',
        'customer_order_view', 'customer_order_link_new', 'auth_maintenance_purge', 'auth_check_reason',
        'customer_lookup_core', 'customer_disable_core', 'customer_revoke_sessions_core',
        'customer_release_email_core', 'auth_settings_update_core', 'admin_customer_lookup',
        'admin_customer_disable', 'admin_customer_revoke_sessions', 'admin_customer_release_email',
        'admin_auth_settings_update'];
BEGIN
    IF shop_schema IS NULL OR shop_schema LIKE 'pg\_temp%' OR shop_schema = 'pg_catalog' THEN
        RAISE EXCEPTION 'миграция должна выполняться со схемой магазина в search_path (сейчас: %)', shop_schema;
    END IF;
    IF (SELECT count(*) FROM pg_proc p
        WHERE p.pronamespace = shop_schema::REGNAMESPACE AND p.proname::TEXT = ANY(expected)) <> cardinality(expected) THEN
        RAISE EXCEPTION '0005: создан не весь ожидаемый набор функций';
    END IF;
    FOR fn IN
        SELECT p.oid::REGPROCEDURE FROM pg_proc p
        WHERE p.pronamespace = shop_schema::REGNAMESPACE AND p.proname::TEXT = ANY(expected)
    LOOP
        EXECUTE format('ALTER FUNCTION %s SET search_path = pg_catalog, %I, pg_temp', fn, shop_schema);
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC', fn);
    END LOOP;
END
$$;
