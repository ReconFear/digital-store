"""Снимок и проверки модели безопасности тестовой базы CI (используют prepare_test_db.py и check_test_db.py).

Только чтение. Секретов в снимке нет: имена ролей, атрибуты, ACL, владельцы, версии миграций.
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
    bad_path = _rows(conn, """
        SELECT p.oid::regprocedure::text FROM pg_proc p
        WHERE p.pronamespace = 'public'::regnamespace AND p.prosecdef
          AND coalesce(array_to_string(p.proconfig, ';'), '') <> 'search_path=pg_catalog, public, pg_temp'""")
    if bad_path:
        problems.append(f"SECURITY DEFINER без доверенного search_path: {[r[0] for r in bad_path]}")
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
    if [v for v, _ in expected][:5] != ["0000", "0001", "0002", "0003", "0004"]:
        problems.append(f"в репозитории неожиданный набор миграций: {[v for v, _ in expected]}")
    if applied != expected:
        problems.append(f"применённые миграции {applied} не совпадают с файлами {expected}")
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
