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
                     заказа, запрос возврата. Выдаётся конкретным учётным записям (GRANT ... TO).

Изменение финансовых статусов и истории — только через SECURITY DEFINER-функции миграции
0003 (путь поиска закреплён: pg_catalog, <схема>, pg_temp; EXECUTE у PUBLIC отозван).

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
)
# Обработчик платежей и исполнения: читает попытки и очередь, обновляет очередь и задачи
# исполнения; учитывает оплату только через payment_record_provider_state.
WORKER_SELECT = ("products", "orders", "order_items", "fulfillments", "payments", "provider_events")
WORKER_FUNCTIONS = (
    "payment_attempt_bind(bigint, text, text, boolean)",
    "payment_attempt_mark(bigint, text, text)",
    "payment_attempt_touch(bigint, text)",
    "payment_record_provider_state(bigint, text, text, bigint, text, boolean, boolean, text, text)",
)
FULFILLMENT_COLUMNS = "status, attempts, last_error, next_attempt_at, updated_at, fulfilled_at"
EVENT_COLUMNS = ("status, attempts, last_error, next_attempt_at, processed_at, "
                 "lease_token, lease_expires_at, recheck_requested")
# Администратор: читает всё, кроме хэшей секретов заказов.
ADMIN_SELECT = ("products", "orders", "order_items", "fulfillments", "payments", "money_operations",
                "provider_events", "refunds", "audit_log", "schema_migrations")
ADMIN_FUNCTIONS = (
    "admin_retry_paid_order(integer)",
    "request_refund(bigint, bigint, text)",
    "admin_resolve_payment(bigint, text, text)",
)
ALL_TABLES = ("products", "orders", "order_items", "fulfillments", "payments", "money_operations",
              "provider_events", "refunds", "audit_log", "order_access", "schema_migrations")


def role_names(prefix):
    return {kind: f"{prefix}_{kind}" for kind in ("migrator", "app", "worker", "admin")}


def _check(name, value):
    if not IDENT.match(value):
        raise ValueError(f"{name}: допустимы строчные латинские буквы, цифры и _ (получено {value!r})")


def grants_sql(schema="public", prefix="digital_store"):
    """GRANT-часть: права ролей на объекты схемы (роли уже должны существовать)."""
    _check("schema", schema)
    _check("prefix", prefix)
    r = role_names(prefix)
    s = schema
    out = [
        f"-- Права для схемы {s}. Выполнять после миграции 0003 под владельцем схемы или суперпользователем.",
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
        WHERE n.nspname = '{schema}' AND c.relkind IN ('r', 'p') AND c.relname IN ({tables})
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


def database_sql(database, prefix="digital_store"):
    _check("database", database)
    _check("prefix", prefix)
    r = role_names(prefix)
    return "\n".join([
        f"-- Подключение к базе {database}: только ролям магазина (и суперпользователю).",
        f"REVOKE CONNECT, TEMPORARY ON DATABASE {database} FROM PUBLIC;",
        f"GRANT CONNECT ON DATABASE {database} TO {r['migrator']}, {r['app']}, {r['worker']}, {r['admin']};",
    ]) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description="Печатает SQL ролей Digital Store. Ничего не применяет.")
    parser.add_argument("--schema", default="public")
    parser.add_argument("--prefix", default="digital_store")
    parser.add_argument("--database", help="имя базы для REVOKE/GRANT CONNECT (необязательно)")
    parser.add_argument("--part", choices=("all", "roles", "database", "ownership", "grants"), default="all",
                        help="all = roles + database (если указана) + grants; ownership — только явно")
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
    sys.stdout.write("\n".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
