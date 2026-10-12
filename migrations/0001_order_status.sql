-- 0001 — статус заказа.
--
-- Тот же SQL, что в исторической migration_001_order_status.sql (её уже применяли
-- вручную к digital_store и digital_store_test), но без BEGIN/COMMIT —
-- транзакцией управляет migrate.py.
--
-- Повторное применение безопасно:
--   * если колонки status нет, она создаётся, а существующие заказы получают 'legacy';
--   * если колонка уже есть (миграцию применяли вручную), ADD COLUMN IF NOT EXISTS
--     ничего не делает, а статусы существующих заказов не меняются;
--   * DEFAULT 'pending' для новых заказов устанавливается в обоих случаях.

ALTER TABLE orders
ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'legacy'
CHECK (status IN ('pending', 'paid', 'cancelled', 'legacy'));

ALTER TABLE orders
ALTER COLUMN status SET DEFAULT 'pending';

-- Проверка: колонка именно такая, как ожидает код.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'orders'
          AND column_name = 'status'
          AND data_type = 'text'
          AND is_nullable = 'NO'
          AND column_default = '''pending''::text'
    ) THEN
        RAISE EXCEPTION 'orders.status отличается от ожидаемого (text NOT NULL DEFAULT ''pending'')';
    END IF;

    -- Если колонку status когда-то добавили вручную без CHECK или с другим CHECK,
    -- ADD COLUMN IF NOT EXISTS её пропустит — поэтому ограничение проверяется отдельно.
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = to_regclass('orders')
          AND contype = 'c'
          AND pg_get_constraintdef(oid) =
              'CHECK ((status = ANY (ARRAY[''pending''::text, ''paid''::text, ''cancelled''::text, ''legacy''::text])))'
    ) THEN
        RAISE EXCEPTION 'у orders.status нет ожидаемого CHECK (pending, paid, cancelled, legacy)';
    END IF;
END
$$;
