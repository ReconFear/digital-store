"""SQL раздельных ролей PostgreSQL для Digital Store (PR 3). Ничего не применяет сам.

    python deploy/db_roles.py --schema public > roles.sql      # посмотреть SQL
    (применяет человек, под суперпользователем, после проверки — пошагово в DB_ROLES.md)

Роли (имена меняются параметром --prefix):

  <prefix>_migrator  LOGIN. Владелец схемы и всех таблиц/функций; только для migrate.py.
                     Пароль задаётся отдельно (ALTER ROLE ... PASSWORD) — здесь паролей нет.
  <prefix>_app       LOGIN. Веб-процесс (uvicorn api:app). Читает каталог и заказы, создаёт
                     заказы, начинает оплату. НЕ может: изменить статус заказа, остатки,
                     цены, платежи, денежные операции; учесть оплату; удалить историю.
  <prefix>_worker    LOGIN. Платёжный обработчик и исполнение заказов (python -m payments.worker).
                     Учитывает оплату только через payment_record_provider_state.
  <prefix>_admin     NOLOGIN-группа для людей: чтение всего, повторное проведение оплаченного
                     заказа, запрос возврата, управление цифровыми ключами (PR5). Выдаётся
                     персональным учётным записям: --part admin-login --login <имя>.

Изменение финансовых статусов и истории — только через SECURITY DEFINER-функции миграции
0003 (путь поиска закреплён: pg_catalog, <схема>, pg_temp; EXECUTE у PUBLIC отозван).

Аккаунты покупателей (PR6, миграция 0005): app вызывает только функции auth_*/customer_*, worker —
auth_maintenance_purge, admin — admin_customer_* и admin_auth_settings_update. Таблицы аккаунтов
(ACCOUNT_TABLES) и схема ext_pgcrypto ни одной из этих ролей не доступны.

PR6: --part extensions — pgcrypto в схеме ext_pgcrypto (USAGE только мигратору); --part app-settings
--database <база> — необязательные параметры роли app только в этой базе. Обе части печатаются только
явно и в --part all не входят.

Скрипт НЕ подключается к базе и НЕ меняет существующие роли. Существующие роли
(например, postgres или прежняя роль приложения) он не трогает: переназначение
владельца — отдельный шаг в DB_ROLES.md, выполняемый вручную.
"""

import argparse
import re
import sys

IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,40}$")

# Веб-приложение: только то, без чего не работают каталог и создание заказов. Секреты заказов,
# платежи, деньги, возвраты, исполнение и очередь событий оно напрямую НЕ читает и не пишет —
# всё через функции, требующие секрет конкретного заказа.
APP_SELECT = ("products", "orders", "order_items")   # orders/order_items: RETURNING и проверки 0002
APP_INSERT = ("orders", "order_items")
APP_SEQUENCES = ("orders_id_seq", "order_items_id_seq")
APP_FUNCTIONS = (
    "order_access_issue(integer, bytea)",
    "order_status_view(integer, bytea)",
    "payment_attempt_start(integer, bytea, text, uuid)",
    "payment_checkout_bind(bigint, bytea, text, text, boolean)",
    "payment_checkout_mark(bigint, bytea, text, text)",
    "provider_event_accept(text, text, text, text)",
    # PR5: выдача ключей покупателю и проверка версий ключей шифрования
    "order_delivery_view(integer, bytea)",
    "encryption_key_check_values()",
    # PR6 (0005): аккаунты покупателей. Таблицы аккаунтов веб-роли недоступны — только эти функции;
    # покупатель всегда выводится из хэша сессии внутри функции.
    "auth_register(text, text, smallint, bytea)",
    "auth_login(text, text, smallint, text, smallint, bytea, bytea)",
    "auth_password_change(bytea, text, smallint, text, smallint, text, smallint, bytea)",
    "auth_session_resolve(bytea)",
    "auth_session_touch(bytea)",
    "auth_session_revoke(bytea)",
    "auth_sessions_revoke_all(bytea)",
    "customer_profile_view(bytea)",
    "customer_orders_view(bytea)",
    "customer_order_view(bytea, integer)",
    "customer_order_link_new(bytea, integer)",
)
# PR5: продаваемый остаток (каталог и создание заказа). Таблицы ключей веб-роли недоступны.
APP_SELECT_VIEWS = ("product_sellable_stock",)
# Обработчик платежей и исполнения: читает попытки и очередь, обновляет очередь и задачи
# исполнения; учитывает оплату только через payment_record_provider_state.
WORKER_SELECT = ("products", "orders", "order_items", "fulfillments", "payments", "provider_events")
WORKER_FUNCTIONS = (
    "payment_attempt_bind(bigint, text, text, boolean)",
    "payment_attempt_mark(bigint, text, text)",
    "payment_attempt_touch(bigint, text)",
    "payment_record_provider_state(bigint, text, text, bigint, text, boolean, boolean, text, text)",
    # PR5: назначение ключей задаче (без секретов шифрования) и проверка триггера задач
    "digital_keys_assign(bigint)",
    "digital_keys_task_complete(bigint)",
    # PR6: очистка истёкших счётчиков попыток и сессий
    "auth_maintenance_purge()",
)
FULFILLMENT_COLUMNS = "status, attempts, last_error, status_note, next_attempt_at, updated_at, fulfilled_at"
EVENT_COLUMNS = ("status, attempts, last_error, next_attempt_at, processed_at, "
                 "lease_token, lease_expires_at, recheck_requested")
# Администратор: читает всё, кроме хэшей секретов заказов.
ADMIN_SELECT = ("products", "orders", "order_items", "fulfillments", "payments", "money_operations",
                "provider_events", "refunds", "audit_log", "schema_migrations")
ADMIN_FUNCTIONS = (
    "admin_retry_paid_order(integer)",
    "request_refund(bigint, bigint, text)",
    "admin_resolve_payment(bigint, text, text)",
    # PR5 (каждая дополнительно проверяет членство вошедшего в группе администраторов)
    "admin_import_digital_keys(integer, text, text[], text[], text[], text[], text[], text, boolean)",
    "admin_withdraw_digital_key(uuid, text)",
    "admin_resume_fulfillment(bigint, text, boolean)",
    "admin_cancel_fulfillment(bigint, text)",
    "admin_reconcile_digital_stock(integer)",
    "admin_register_encryption_key(text, text, text)",
    "admin_digital_keys_keyring_sample()",
    "admin_whoami()",
    "encryption_key_check_values()",
    # PR6 (каждая проверяет администратора через shop_require_admin; таблиц аккаунтов администратор не читает)
    "admin_customer_lookup(text)",
    "admin_customer_disable(bigint, text)",
    "admin_customer_revoke_sessions(bigint, text)",
    "admin_customer_release_email(bigint, text)",
    "admin_auth_settings_update(boolean, boolean)",
)
ADMIN_SELECT_PR5 = ("digital_key_batches", "encryption_key_registry", "digital_key_stock", "fulfillment_issues",
                    "product_sellable_stock", "shop_security_settings")
# Ключи: администратор видит учёт, но не шифротексты, nonce и отпечатки.
ADMIN_KEY_COLUMNS = ("id, public_id, product_id, batch_id, enc_key_id, fingerprint_key_id, status, fulfillment_id, "
                     "order_item_id, imported_at, assigned_at, withdrawn_at, withdraw_reason")
ALL_TABLES = ("products", "orders", "order_items", "fulfillments", "payments", "money_operations",
              "provider_events", "refunds", "audit_log", "order_access", "schema_migrations",
              # PR5
              "shop_security_settings", "encryption_key_registry", "digital_key_batches", "digital_keys",
              "product_sellable_stock", "digital_key_stock", "fulfillment_issues",
              # PR6
              "auth_settings", "customers", "customer_sessions", "customer_orders", "auth_throttle")
# PR6: таблицы аккаунтов (хэши паролей, сессии, принадлежность заказов, счётчики, настройки). Ни app, ни
# worker, ни admin прав на них не получают (REV4 §5) — проверяет ci/db_invariants.py.
ACCOUNT_TABLES = ("auth_settings", "customers", "customer_sessions", "customer_orders", "auth_throttle")


def role_names(prefix):
    return {kind: f"{prefix}_{kind}" for kind in ("migrator", "app", "worker", "admin")}


def _check(name, value):
    # fullmatch, а не match: "$" в match пропускает завершающий перевод строки ("shop\n").
    if not IDENT.fullmatch(value):
        raise ValueError(f"{name}: допустимы строчные латинские буквы, цифры и _ (получено {value!r})")


def grants_sql(schema="public", prefix="digital_store"):
    """GRANT-часть: права ролей на объекты схемы (роли уже должны существовать)."""
    _check("schema", schema)
    _check("prefix", prefix)
    r = role_names(prefix)
    s = schema
    out = [
        f"-- Права для схемы {s}. Выполнять после миграции 0005 под владельцем схемы или суперпользователем.",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA {s} FROM PUBLIC;",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA {s} FROM PUBLIC;",
        f"REVOKE CREATE ON SCHEMA {s} FROM PUBLIC;",
        f"GRANT USAGE ON SCHEMA {s} TO {r['app']}, {r['worker']}, {r['admin']};",
        "",
        "-- веб-приложение",
        f"GRANT SELECT ON {', '.join(f'{s}.{t}' for t in APP_SELECT)} TO {r['app']};",
        f"GRANT INSERT ON {', '.join(f'{s}.{t}' for t in APP_INSERT)} TO {r['app']};",
        # Закрытие состава заказа триггером при COMMIT (0002) выполняется правами приложения.
        f"GRANT UPDATE (items_sealed) ON {s}.orders TO {r['app']};",
        f"GRANT USAGE ON SEQUENCE {', '.join(f'{s}.{q}' for q in APP_SEQUENCES)} TO {r['app']};",
    ]
    out += [f"GRANT EXECUTE ON FUNCTION {s}.{fn} TO {r['app']};" for fn in APP_FUNCTIONS]
    out.append(f"GRANT SELECT ON {', '.join(f'{s}.{t}' for t in APP_SELECT_VIEWS)} TO {r['app']};")
    out += [
        "",
        "-- платёжный обработчик и исполнение заказов",
        f"GRANT SELECT ON {', '.join(f'{s}.{t}' for t in WORKER_SELECT)} TO {r['worker']};",
        f"GRANT UPDATE ({FULFILLMENT_COLUMNS}) ON {s}.fulfillments TO {r['worker']};",
        f"GRANT UPDATE ({EVENT_COLUMNS}) ON {s}.provider_events TO {r['worker']};",
    ]
    out += [f"GRANT EXECUTE ON FUNCTION {s}.{fn} TO {r['worker']};" for fn in WORKER_FUNCTIONS]
    out += [
        "",
        "-- администратор (люди)",
        f"GRANT SELECT ON {', '.join(f'{s}.{t}' for t in ADMIN_SELECT)} TO {r['admin']};",
    ]
    out += [f"GRANT EXECUTE ON FUNCTION {s}.{fn} TO {r['admin']};" for fn in ADMIN_FUNCTIONS]
    out += [
        f"GRANT SELECT ON {', '.join(f'{s}.{t}' for t in ADMIN_SELECT_PR5)} TO {r['admin']};",
        f"GRANT SELECT ({ADMIN_KEY_COLUMNS}) ON {s}.digital_keys TO {r['admin']};",
        "",
        "-- PR5: какая группа — администраторы магазина. Проверяется в базе каждой admin_*-функцией",
        "-- (членство вошедшего с наследованием прав + EXECUTE). Выполнять владельцем схемы (мигратором).",
        f"INSERT INTO {s}.shop_security_settings (singleton, admin_group) VALUES (TRUE, '{r['admin']}')",
        f"    ON CONFLICT (singleton) DO UPDATE SET admin_group = EXCLUDED.admin_group, updated_at = now();",
    ]
    return "\n".join(out) + "\n"


def create_roles_sql(prefix="digital_store"):
    """CREATE ROLE для новых ролей. Пароли — отдельно, вручную (здесь их нет и не будет)."""
    _check("prefix", prefix)
    r = role_names(prefix)
    return "\n".join([
        "-- Новые роли. Существующие роли не изменяются. Пароли задать вручную:",
        f"--   ALTER ROLE {r['migrator']} PASSWORD '...';  (то же для _app и _worker)",
        f"CREATE ROLE {r['migrator']} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;",
        f"CREATE ROLE {r['app']} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;",
        f"CREATE ROLE {r['worker']} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;",
        f"CREATE ROLE {r['admin']} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;",
    ]) + "\n"


SHOP_FUNCTIONS = (
    # 0002 (триггеры ядра)
    "products_sync_legacy_price", "orders_fill_legacy_columns", "orders_create_legacy_item",
    "orders_protect_history", "orders_guard_status", "orders_protect_delete",
    "order_items_protect_history", "order_history_protect_truncate",
    "order_items_guard_insert", "orders_check_composition",
    # 0003 (платежи)
    "payments_guard", "refunds_guard", "financial_history_protect", "financial_history_protect_truncate",
    "apply_paid_order", "order_access_issue", "order_status_view", "payment_attempt_start",
    "payment_attempt_bind", "payment_attempt_mark", "payment_checkout_bind", "payment_checkout_mark",
    "payment_attempt_touch", "provider_event_accept", "payment_record_provider_state",
    "admin_resolve_payment", "admin_retry_paid_order", "request_refund",
    # 0004 (выдача цифровых ключей)
    "digital_key_stock_counts", "digital_keys_task_complete", "digital_keys_guard", "digital_keys_protect_truncate",
    "digital_key_records_protect", "fulfillments_guard", "products_digital_stock_guard",
    "digital_keys_assign", "order_delivery_view", "encryption_key_check_values", "shop_require_admin",
    "shop_is_owner_role", "digital_keys_import_core", "digital_keys_withdraw_core", "fulfillment_resume_core",
    "fulfillment_cancel_core", "digital_stock_reconcile_core", "encryption_key_register_core",
    "digital_keys_keyring_sample_core", "admin_import_digital_keys", "admin_withdraw_digital_key",
    "admin_resume_fulfillment", "admin_cancel_fulfillment", "admin_reconcile_digital_stock",
    "admin_register_encryption_key", "admin_digital_keys_keyring_sample", "admin_whoami",
    # 0005 (аккаунты покупателей, PR6)
    "auth_settings_dummy_hash", "customers_hash_cost_guard", "customer_sessions_guard", "customer_orders_protect",
    "customer_orders_protect_truncate", "auth_require_read_committed", "auth_check_email",
    "auth_check_not_reserved", "auth_check_proof", "auth_check_proof_pair", "auth_check_token",
    "customer_session_customer", "auth_session_refusal", "customer_sessions_invalidate", "auth_register",
    "auth_login", "auth_password_change", "auth_session_resolve", "auth_session_touch", "auth_session_revoke",
    "auth_sessions_revoke_all", "customer_profile_view", "customer_orders_view", "customer_order_view",
    "customer_order_link_new", "auth_maintenance_purge", "auth_check_reason", "customer_lookup_core",
    "customer_disable_core", "customer_revoke_sessions_core", "customer_release_email_core",
    "auth_settings_update_core", "admin_customer_lookup", "admin_customer_disable", "admin_customer_revoke_sessions",
    "admin_customer_release_email", "admin_auth_settings_update",
)


def ownership_sql(schema="public", prefix="digital_store"):
    """Передать объекты магазина роли-мигратору (для СУЩЕСТВУЮЩЕЙ базы, созданной под postgres).

    Только перечисленные таблицы и функции магазина; REASSIGN OWNED не используется, чтобы не
    задеть чужие и системные объекты. SECURITY DEFINER-функции после этого выполняются с правами
    мигратора (не суперпользователя) — так и должно быть.
    """
    _check("schema", schema)
    _check("prefix", prefix)
    migrator = role_names(prefix)["migrator"]
    tables = ", ".join(f"'{t}'" for t in ALL_TABLES)
    functions = ", ".join(f"'{f}'" for f in SHOP_FUNCTIONS)
    return f"""-- Владелец объектов магазина в схеме {schema} -> {migrator}. Выполнять под суперпользователем.
GRANT USAGE, CREATE ON SCHEMA {schema} TO {migrator};
DO $$
DECLARE
    obj TEXT;
BEGIN
    FOR obj IN
        SELECT format('%I.%I', n.nspname, c.relname)
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = '{schema}' AND c.relkind IN ('r', 'p', 'v') AND c.relname IN ({tables})
    LOOP
        -- Вместе с таблицей меняется владелец её последовательностей (SERIAL/IDENTITY).
        EXECUTE format('ALTER TABLE %s OWNER TO {migrator}', obj);
    END LOOP;
    FOR obj IN
        SELECT p.oid::regprocedure::text
        FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = '{schema}' AND p.proname IN ({functions})
    LOOP
        EXECUTE format('ALTER FUNCTION %s OWNER TO {migrator}', obj);
    END LOOP;
END
$$;
"""


def admin_login_sql(login, prefix="digital_store"):
    """Персональная учётная запись администратора (PR5). Пароль — отдельно, вручную.

    WITH INHERIT TRUE, SET FALSE (PostgreSQL 16+): права группы действуют сразу (их и проверяют
    admin_*-функции: членство с наследованием + EXECUTE), а SET ROLE в группу запрещён. Для
    PostgreSQL 15 и ниже — DB_ROLES.md, раздел «Администраторы».
    """
    _check("prefix", prefix)
    _check("login", login)
    group = role_names(prefix)["admin"]
    if login in role_names(prefix).values():
        raise ValueError("login: нужна отдельная персональная роль, не роль магазина")
    return "\n".join([
        f"-- Администратор {login}. Пароль задать вручную: ALTER ROLE {login} PASSWORD '...';",
        f"CREATE ROLE {login} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS INHERIT;",
        f"GRANT {group} TO {login} WITH INHERIT TRUE, SET FALSE;",
    ]) + "\n"


def database_sql(database, prefix="digital_store"):
    _check("database", database)
    _check("prefix", prefix)
    r = role_names(prefix)
    return "\n".join([
        f"-- Подключение к базе {database}: только ролям магазина (и суперпользователю).",
        f"REVOKE CONNECT, TEMPORARY ON DATABASE {database} FROM PUBLIC;",
        f"GRANT CONNECT ON DATABASE {database} TO {r['migrator']}, {r['app']}, {r['worker']}, {r['admin']};",
    ]) + "\n"


# PR6: схема расширения pgcrypto (bcrypt для паролей покупателей). Имя закреплено архитектурой P1:
# функции 0005 вызывают ext_pgcrypto.crypt/gen_salt с явной схемой, в search_path её нет.
PGCRYPTO_SCHEMA = "ext_pgcrypto"


def extensions_sql(prefix="digital_store"):
    """pgcrypto в отдельной схеме (PR6). Выполняет владелец базы или суперпользователь ДО миграции 0005.

    USAGE на схему — только мигратору: SECURITY DEFINER-функции магазина выполняются с его правами.
    app, worker и группа admin прав на схему не получают. IF NOT EXISTS не используется намеренно:
    уже существующая схема (возможно, чужая) или расширение в другой схеме должны дать ошибку.
    """
    _check("prefix", prefix)
    migrator = role_names(prefix)["migrator"]
    s = PGCRYPTO_SCHEMA
    return "\n".join([
        f"-- PR6: pgcrypto в схеме {s}. Выполнять владельцем базы (или суперпользователем) в нужной базе",
        "-- ДО миграции 0005. Без IF NOT EXISTS: существующая схема или расширение в другой схеме — ошибка,",
        "-- разобраться вручную. USAGE — только мигратору; app, worker и admin прав на схему не получают.",
        f"CREATE SCHEMA {s};",
        f"CREATE EXTENSION pgcrypto WITH SCHEMA {s};",
        f"REVOKE ALL ON SCHEMA {s} FROM PUBLIC;",
        f"GRANT USAGE ON SCHEMA {s} TO {migrator};",
    ]) + "\n"


def app_settings_sql(database, prefix="digital_store"):
    """Необязательные параметры роли app (PR6, REV4 Б4). Только по решению владельца, вручную.

    Меняет параметры СУЩЕСТВУЮЩЕЙ роли <prefix>_app только в указанной базе (ALTER ROLE ... IN DATABASE);
    база обязательна — параметры роли на весь кластер не генерируются. Другие роли не трогает.
    log_parameter_max_length* — параметры уровня суперпользователя. statement_timeout — только
    комментарием: значение зависит от замера стоимости bcrypt (Г7).
    """
    if not database:
        raise ValueError("database: имя базы обязательно (ALTER ROLE ... IN DATABASE), без него SQL не генерируется")
    _check("database", database)
    _check("prefix", prefix)
    app = role_names(prefix)["app"]
    target = f"{app} IN DATABASE {database}"
    return "\n".join([
        f"-- Необязательно, только по решению владельца. Меняет параметры существующей роли {app} в базе {database}.",
        "-- log_parameter_max_length* — параметры уровня суперпользователя: выполнять под суперпользователем.",
        "-- Откат: ALTER ROLE ... RESET <параметр>;",
        f"ALTER ROLE {target} SET idle_in_transaction_session_timeout = '30s';",
        f"ALTER ROLE {target} SET log_parameter_max_length = 0;",
        f"ALTER ROLE {target} SET log_parameter_max_length_on_error = 0;",
        "-- statement_timeout задаётся вручную после замера Г7: больше lock_timeout + 2 x время bcrypt, с запасом.",
        f"--   ALTER ROLE {target} SET statement_timeout = '<по замеру>';",
    ]) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Печатает SQL ролей Digital Store. Ничего не применяет.")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--prefix", default="digital_store")
    parser.add_argument("--database", help="имя базы для REVOKE/GRANT CONNECT (необязательно); "
                                           "для --part app-settings обязательно")
    parser.add_argument("--part", choices=("all", "roles", "database", "ownership", "grants", "admin-login",
                                           "extensions", "app-settings"),
                        default="all",
                        help="all = roles + database (если указана) + grants; ownership, admin-login, "
                             "extensions и app-settings — только явно")
    parser.add_argument("--login", help="для --part admin-login: имя персональной роли администратора")
    args = parser.parse_args(argv)
    parts = []
    if args.part in ("all", "roles"):
        parts.append(create_roles_sql(args.prefix))
    if args.part in ("all", "database") and args.database:
        parts.append(database_sql(args.database, args.prefix))
    if args.part == "ownership":
        parts.append(ownership_sql(args.schema, args.prefix))
    if args.part in ("all", "grants"):
        parts.append(grants_sql(args.schema, args.prefix))
    if args.part == "admin-login":
        if not args.login:
            parser.error("--part admin-login требует --login")
        parts.append(admin_login_sql(args.login, args.prefix))
    if args.part == "extensions":
        parts.append(extensions_sql(args.prefix))
    if args.part == "app-settings":
        if not args.database:
            parser.error("--part app-settings требует --database (параметры роли задаются только для одной базы)")
        parts.append(app_settings_sql(args.database, args.prefix))
    sys.stdout.write("\n".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
