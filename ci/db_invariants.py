"""Снимок и проверки модели безопасности тестовой базы CI (используют prepare_test_db.py и check_test_db.py).

Только чтение. Секретов в снимке нет: имена ролей, атрибуты, ACL, владельцы, версии миграций, расширения.
PR6: дополнительно проверяются pgcrypto в ext_pgcrypto, права ролей магазина на базу, схему и таблицы
аккаунтов, точный перечень EXECUTE каждой роли, параметры функций и флаги аккаунтов (_pr6_problems).
"""

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# PR6 (0005, ADDENDUM2 §1.2): функции с этапом блокировок в подтранзакции — единственные, у которых кроме
# search_path разрешён ещё один параметр, lock_timeout = 2s.
LOCK_TIMEOUT_FUNCTIONS = {"auth_register", "auth_login", "auth_password_change", "auth_sessions_revoke_all",
                          "customer_disable_core", "customer_revoke_sessions_core", "customer_release_email_core"}


class InvariantError(Exception):
    pass


def require_disposable_target(database, host):
    """Отказ для всего, что не похоже на одноразовую тестовую базу в локальном контейнере CI."""
    if database in ("digital_store", "postgres", "template0", "template1") or not database.endswith("_test"):
        raise InvariantError(f"база {database!r}: CI работает только с отдельной базой *_test")
    if host not in LOCAL_HOSTS and not host.startswith("/"):
        raise InvariantError(f"хост {host!r}: CI подключается только к локальному контейнеру PostgreSQL")


def connect(database, user, password, autocommit=False):
    return psycopg.connect(host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"), dbname=database,
                           user=user, password=password, autocommit=autocommit)


def _rows(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [list(row) for row in cur.fetchall()]


def snapshot(conn):
    """Состояние, которое тесты НЕ должны менять (данные таблиц не входят).

    ACL сравниваются как ДЕЙСТВУЮЩИЕ права: NULL («по умолчанию») раскрывается через acldefault(), поэтому
    равнозначная запись прав не считается изменением, а любая новая или исчезнувшая привилегия — считается."""
    db = _rows(conn, "SELECT current_database()")[0][0]
    snap = {
        "roles": _rows(conn, """
            SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication,
                   rolbypassrls
            FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%' ORDER BY rolname"""),
        "memberships": _rows(conn, """
            SELECT r.rolname, m.rolname, a.admin_option, a.inherit_option, a.set_option
            FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid JOIN pg_roles m ON m.oid = a.member
            WHERE r.rolname NOT LIKE 'pg\\_%' ORDER BY 1, 2"""),
        "database": _rows(conn, """
            SELECT pg_get_userbyid(datdba), coalesce(datacl, acldefault('d', datdba))::text FROM pg_database
            WHERE datname = current_database()"""),
        "schemas": _rows(conn, """
            SELECT nspname, pg_get_userbyid(nspowner), coalesce(nspacl, acldefault('n', nspowner))::text FROM pg_namespace
            WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema' ORDER BY 1"""),
        "relations": _rows(conn, """
            SELECT c.relname, c.relkind::text, pg_get_userbyid(c.relowner),
                   coalesce(c.relacl, acldefault(CASE WHEN c.relkind = 'S' THEN 's' ELSE 'r' END::"char",
                                                 c.relowner))::text,
                   c.relrowsecurity
            FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind IN ('r', 'p', 'v', 'm', 'S')
            ORDER BY 1"""),
        "column_acls": _rows(conn, """
            SELECT c.relname, a.attname, a.attacl::text FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
            WHERE c.relnamespace = 'public'::regnamespace AND a.attacl IS NOT NULL ORDER BY 1, 2"""),
        "functions": _rows(conn, """
            SELECT p.oid::regprocedure::text, pg_get_userbyid(p.proowner), p.prosecdef,
                   coalesce(p.proacl, acldefault('f', p.proowner))::text,
                   coalesce(array_to_string(p.proconfig, ';'), '')
            FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace ORDER BY 1"""),
        "default_acls": _rows(conn, """
            SELECT pg_get_userbyid(defaclrole), defaclobjtype::text, defaclacl::text FROM pg_default_acl ORDER BY 1, 2"""),
        "triggers": _rows(conn, """
            SELECT tgrelid::regclass::text, tgname, tgenabled::text FROM pg_trigger
            WHERE NOT tgisinternal ORDER BY 1, 2"""),
        "admin_group": _rows(conn, "SELECT admin_group FROM shop_security_settings"),
        "migrations": _rows(conn, "SELECT version, checksum FROM schema_migrations ORDER BY version"),
        # PR6: расширения и права на функции pgcrypto (тесты не должны их менять).
        "extensions": _rows(conn, """
            SELECT e.extname, n.nspname, pg_get_userbyid(e.extowner), e.extversion FROM pg_extension e
            JOIN pg_namespace n ON n.oid = e.extnamespace ORDER BY 1"""),
        "pgcrypto_functions": _rows(conn, """
            SELECT p.oid::regprocedure::text, pg_get_userbyid(p.proowner),
                   coalesce(p.proacl, acldefault('f', p.proowner))::text
            FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'ext_pgcrypto' ORDER BY 1"""),
    }
    snap["database_name"] = db
    return snap


def check_invariants(conn, prefix):
    """Модель безопасности, независимо от снимка. Возвращает список нарушений (пусто — всё в порядке)."""
    import migrate

    problems = []
    migrator, admin = f"{prefix}_migrator", f"{prefix}_admin"

    def one(sql, params=None):
        return _rows(conn, sql, params)[0][0]

    if one("SELECT rolsuper FROM pg_roles WHERE rolname = %s", (migrator,)) is not False:
        problems.append(f"{migrator} должен существовать и НЕ быть суперпользователем")
    for kind in ("app", "worker", "admin"):
        row = _rows(conn, "SELECT rolsuper, rolcreaterole, rolbypassrls FROM pg_roles WHERE rolname = %s",
                    (f"{prefix}_{kind}",))
        if not row or any(row[0]):
            problems.append(f"{prefix}_{kind}: нет роли или опасные атрибуты (SUPERUSER/CREATEROLE/BYPASSRLS)")
    if one("SELECT rolcanlogin FROM pg_roles WHERE rolname = %s", (admin,)) is not False:
        problems.append(f"{admin} должна быть NOLOGIN-группой")

    foreign = _rows(conn, """
        SELECT c.relname FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S') AND pg_get_userbyid(c.relowner) <> %s ORDER BY 1""", (migrator,))
    if foreign:
        problems.append(f"объекты public не принадлежат {migrator}: {[r[0] for r in foreign]}")
    foreign = _rows(conn, """
        SELECT p.oid::regprocedure::text FROM pg_proc p WHERE p.pronamespace = 'public'::regnamespace
          AND pg_get_userbyid(p.proowner) <> %s ORDER BY 1""", (migrator,))
    if foreign:
        problems.append(f"функции public не принадлежат {migrator}: {[r[0] for r in foreign]}")
    definers_by_super = _rows(conn, """
        SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_roles r ON r.oid = p.proowner
        WHERE p.prosecdef AND r.rolsuper""")
    if definers_by_super:
        problems.append(f"SECURITY DEFINER-функции суперпользователя: {[r[0] for r in definers_by_super]}")
    public_exec = _rows(conn, """
        SELECT p.oid::regprocedure::text FROM pg_proc p
        WHERE p.pronamespace = 'public'::regnamespace AND p.prosecdef
          AND EXISTS (SELECT 1 FROM aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a
                      WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE')""")
    if public_exec:
        problems.append(f"PUBLIC может выполнять SECURITY DEFINER-функции: {[r[0] for r in public_exec]}")
    # Каждая функция схемы магазина (не только SECURITY DEFINER): ровно один search_path доверенного вида
    # (REV5 §5.2 п.5). Других параметров нет, кроме lock_timeout = 2s у функций с этапом блокировок 0005
    # (ADDENDUM2 §1.2): любой другой SET в определении функции — нарушение.
    for signature, name, config in _rows(conn, """
            SELECT p.oid::regprocedure::text, p.proname, coalesce(p.proconfig, '{}'::text[]) FROM pg_proc p
            WHERE p.pronamespace = 'public'::regnamespace ORDER BY 1"""):
        paths = [c for c in config if c.startswith("search_path=")]
        others = sorted(c for c in config if not c.startswith("search_path="))
        if paths != ["search_path=pg_catalog, public, pg_temp"]:
            problems.append(f"функция без доверенного search_path (ровно один, pg_catalog, public, pg_temp): "
                            f"{signature} {config}")
        if others != (["lock_timeout=2s"] if name in LOCK_TIMEOUT_FUNCTIONS else []):
            problems.append(f"неожиданные параметры функции {signature}: {others}")
    if one("""SELECT EXISTS (SELECT 1 FROM pg_database d, aclexplode(coalesce(d.datacl, acldefault('d', d.datdba))) a
                            WHERE d.datname = current_database() AND a.grantee = 0
                              AND a.privilege_type = 'CONNECT')"""):
        problems.append("PUBLIC имеет CONNECT к тестовой базе")
    public_tables = _rows(conn, """
        SELECT c.relname, a.privilege_type FROM pg_class c, aclexplode(c.relacl) a
        WHERE c.relnamespace = 'public'::regnamespace AND a.grantee = 0 ORDER BY 1, 2""")
    if public_tables:
        problems.append(f"PUBLIC имеет права на таблицы: {public_tables}")
    if one("""SELECT EXISTS (SELECT 1 FROM pg_namespace n, aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
                            WHERE n.nspname = 'public' AND a.grantee = 0 AND a.privilege_type = 'CREATE')"""):
        problems.append("PUBLIC может создавать объекты в схеме public")
    groups = _rows(conn, "SELECT admin_group FROM shop_security_settings")
    if groups != [[admin]]:
        problems.append(f"shop_security_settings.admin_group = {groups}, ожидалось [[{admin!r}]]")

    expected = [[m.version, m.checksum] for m in migrate.discover()]
    applied = _rows(conn, "SELECT version, checksum FROM schema_migrations ORDER BY version")
    if [v for v, _ in expected][:6] != ["0000", "0001", "0002", "0003", "0004", "0005"]:
        problems.append(f"в репозитории неожиданный набор миграций: {[v for v, _ in expected]}")
    if applied != expected:
        problems.append(f"применённые миграции {applied} не совпадают с файлами {expected}")

    problems += _pr6_problems(conn, prefix, one)
    return problems


def _pr6_problems(conn, prefix, one):
    """PR6 (REV5 §5.2, ADDENDUM §2, ADDENDUM2 §2): pgcrypto, права ролей магазина, таблицы аккаунтов."""
    from deploy import db_roles

    problems = []
    names = db_roles.role_names(prefix)
    shop_roles = [names["app"], names["worker"], names["admin"]]

    # pgcrypto установлен именно в ext_pgcrypto; схема принадлежит владельцу базы; в ней только объекты
    # расширения; USAGE — только у мигратора, CREATE — ни у кого, кроме владельца схемы.
    ext = _rows(conn, """
        SELECT n.nspname, pg_get_userbyid(n.nspowner),
               (SELECT pg_get_userbyid(d.datdba) FROM pg_database d WHERE d.datname = current_database())
        FROM pg_extension e JOIN pg_namespace n ON n.oid = e.extnamespace WHERE e.extname = 'pgcrypto'""")
    if not ext or ext[0][0] != "ext_pgcrypto":
        problems.append(f"pgcrypto должен быть установлен в схеме ext_pgcrypto (сейчас: {ext})")
    else:
        if ext[0][1] != ext[0][2]:
            problems.append(f"владелец ext_pgcrypto ({ext[0][1]}) — не владелец базы ({ext[0][2]})")
        acl = _rows(conn, """
            SELECT coalesce(r.rolname, 'PUBLIC'), a.privilege_type
            FROM pg_namespace n
            CROSS JOIN LATERAL aclexplode(coalesce(n.nspacl, acldefault('n', n.nspowner))) a
            LEFT JOIN pg_roles r ON r.oid = a.grantee
            WHERE n.nspname = 'ext_pgcrypto' AND a.grantee <> n.nspowner ORDER BY 1, 2""")
        if acl != [[names["migrator"], "USAGE"]]:
            problems.append(f"права на ext_pgcrypto: ожидается только USAGE у {names['migrator']}, сейчас {acl}")
        foreign = _rows(conn, """
            SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'ext_pgcrypto' AND NOT EXISTS (
                SELECT 1 FROM pg_depend d JOIN pg_extension e ON e.oid = d.refobjid
                WHERE d.classid = 'pg_proc'::regclass AND d.objid = p.oid
                  AND d.refclassid = 'pg_extension'::regclass AND d.deptype = 'e' AND e.extname = 'pgcrypto')
            ORDER BY 1""")
        if foreign:
            problems.append(f"в ext_pgcrypto функции не из расширения pgcrypto: {[r[0] for r in foreign]}")
        present = {r[0] for r in _rows(conn, """
            SELECT DISTINCT p.proname::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname = 'ext_pgcrypto' AND p.proname IN ('crypt', 'gen_salt', 'gen_random_bytes')""")}
        if present != {"crypt", "gen_salt", "gen_random_bytes"}:
            problems.append(f"в ext_pgcrypto нет нужных функций pgcrypto: {sorted(present)}")

    # Ни одна роль магазина не создаёт объекты: ни CREATE на базу, ни CREATE на схему магазина; у ролей
    # app/worker/admin нет TEMPORARY (временные объекты не появятся даже в pg_temp).
    for role in names.values():
        if one("SELECT has_database_privilege(%s, current_database(), 'CREATE')", (role,)):
            problems.append(f"{role} имеет CREATE на базу")
    for role in shop_roles:
        if one("SELECT has_database_privilege(%s, current_database(), 'TEMPORARY')", (role,)):
            problems.append(f"{role} имеет TEMPORARY на базу")
        if one("SELECT has_schema_privilege(%s, 'public', 'CREATE')", (role,)):
            problems.append(f"{role} может создавать объекты в схеме public")

    # Таблицы аккаунтов недоступны app/worker/admin — ни на уровне таблицы, ни на уровне столбцов.
    for table in db_roles.ACCOUNT_TABLES:
        if one("SELECT to_regclass(%s) IS NULL", (f"public.{table}",)):
            problems.append(f"нет таблицы {table} (миграция 0005)")
            continue
        for role in shop_roles:
            if one("SELECT has_table_privilege(%s, %s, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER') "
                   "OR has_any_column_privilege(%s, %s, 'SELECT, INSERT, UPDATE, REFERENCES')",
                   (role, f"public.{table}", role, f"public.{table}")):
                problems.append(f"{role} имеет права на таблицу аккаунтов {table}")

    # Точный перечень вызываемых (нетриггерных) функций каждой роли = матрица deploy/db_roles.py (REV4 T-A1).
    # Триггерные функции 0002–0004 сохраняют EXECUTE у PUBLIC, но напрямую не вызываются.
    for kind, functions in (("app", db_roles.APP_FUNCTIONS), ("worker", db_roles.WORKER_FUNCTIONS),
                            ("admin", db_roles.ADMIN_FUNCTIONS)):
        actual = {r[0] for r in _rows(conn, """
            SELECT p.proname::text FROM pg_proc p
            WHERE p.pronamespace = 'public'::regnamespace AND p.prorettype <> 'trigger'::regtype
              AND has_function_privilege(%s, p.oid, 'EXECUTE')""", (names[kind],))}
        expected = {f.split("(", 1)[0] for f in functions}
        if actual != expected:
            problems.append(f"{names[kind]}: EXECUTE не совпадает с матрицей db_roles.py: лишние "
                            f"{sorted(actual - expected)}, недостающие {sorted(expected - actual)}")

    # У каждого столбца *_sha256 — CHECK (octet_length(...) = 32) (ADDENDUM §2).
    unchecked = _rows(conn, """
        SELECT c.relname || '.' || a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
        WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped
          AND a.attname LIKE '%%sha256'
          AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid = c.oid AND k.contype = 'c'
                          AND strpos(pg_get_constraintdef(k.oid), 'octet_length(' || a.attname || ') = 32') > 0)
        ORDER BY 1""")
    if unchecked:
        problems.append(f"столбцы *_sha256 без проверки длины 32 байта: {[r[0] for r in unchecked]}")

    # Аккаунты и регистрация выключены (ADDENDUM2 §2); стоимость хэшей едина (0005, правило стоимости).
    if one("SELECT to_regclass('public.auth_settings') IS NOT NULL"):
        flags = _rows(conn, "SELECT accounts_enabled, registration_open FROM auth_settings")
        if flags != [[False, False]]:
            problems.append(f"auth_settings: ожидалось accounts_enabled = registration_open = FALSE, сейчас {flags}")
        mixed = _rows(conn, """
            SELECT count(*) FROM customers c, auth_settings s
            WHERE substring(c.password_hash FROM '^.2a.([0-9]{2}).')::INTEGER <> s.bcrypt_cost""")
        if mixed != [[0]]:
            problems.append(f"хэши покупателей другой стоимости, чем auth_settings.bcrypt_cost: {mixed}")
    return problems


def diff_snapshots(before, after):
    """Список различий (ключ: было -> стало) для ACL, ролей, владельцев и настроек."""
    out = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) == after.get(key):
            continue
        b = {json.dumps(x, ensure_ascii=False) for x in before.get(key) or []} if isinstance(before.get(key), list) \
            else {json.dumps(before.get(key), ensure_ascii=False)}
        a = {json.dumps(x, ensure_ascii=False) for x in after.get(key) or []} if isinstance(after.get(key), list) \
            else {json.dumps(after.get(key), ensure_ascii=False)}
        for item in sorted(b - a):
            out.append(f"{key}: исчезло {item}")
        for item in sorted(a - b):
            out.append(f"{key}: появилось {item}")
    return out
