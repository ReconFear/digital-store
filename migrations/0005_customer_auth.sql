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
