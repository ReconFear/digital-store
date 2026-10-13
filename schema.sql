SET client_encoding = 'UTF8';

-- Полная текущая схема Digital Store — справочный снимок (после миграции 0003).
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
