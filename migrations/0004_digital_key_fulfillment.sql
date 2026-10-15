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
