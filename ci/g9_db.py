"""G9 (PR6): отдельная одноразовая база и роль CI для семи тестов tests/test_account_disconnect_db.py.

    python ci/g9_db.py prepare --main-baseline <ci-db-baseline.json> --baseline <g9-baseline.json>
    python ci/g9_db.py check --baseline <g9-baseline.json>

prepare — суперпользователем одноразового контейнера, ПОСЛЕ check_test_db.py (её снимок новую роль и базу не видит):
  * роль ds_g9_ci_owner: LOGIN, без SUPERUSER/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS, случайный пароль;
  * база digital_store_g9_ci_test (владелец — суперпользователь контейнера): CONNECT и TEMPORARY у PUBLIC отозваны,
    роли — CONNECT и CREATE (временные схемы ds_mig_* тестов);
  * pgcrypto в схеме ext_pgcrypto этой базы (как deploy/db_roles.py extensions_sql), USAGE — только роли;
  * комментарий-маркер базы ds-g9-disposable-ci; снимок для check.
Пароль: ::add-mask:: до любого вывода, затем G9_DB_PASSWORD в $GITHUB_ENV для следующих шагов; не печатается,
не пишется в снимок, репозиторий и JUnit.

Одноразовость кластера ЭТОГО job доказывается всеми условиями сразу (любое несовпадение — отказ до изменений):
  * GitHub-hosted runner (GITHUB_ACTIONS, RUNNER_ENVIRONMENT) и снимок основной базы в RUNNER_TEMP этого job;
  * подключение: клиент — 127.0.0.1:5432 (conn.info), сервер принял его НЕ на loopback (порт проброшен в
    контейнер сервиса), порт сервера 5432, PostgreSQL 18 (образ postgres:18), запущен меньше 2 часов назад;
  * текущая роль — суперпользователь DB_USER;
  * базы кластера — ровно postgres, template0, template1 и digital_store_roles_test;
  * роли кластера — ровно роли из снимка prepare_test_db.py этого job (он отказывает в непустом кластере), то есть
    кластер был пуст при старте job; роли и базы G9 ещё не существуют.
Одного флага окружения, маркера или loopback-адреса недостаточно — нужны все условия.

check — только чтение (default_transaction_read_only), выполняется всегда: нет временных схем ds_mig_* и
соединений ds-g9-*; атрибуты и членства роли, владелец, ACL, маркер и лимит базы, схемы и расширения базы G9, роли
и базы кластера совпадают со снимком prepare. Нет снимка — ошибка (prepare не выполнялся или не завершился).
К основной базе digital_store_roles_test и к другим базам проекта скрипт не подключается.
"""

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

import psycopg

G9_DB = "digital_store_g9_ci_test"
G9_ROLE = "ds_g9_ci_owner"
MARKER = "ds-g9-disposable-ci"
HOST, PORT = "127.0.0.1", 5432
MAIN_DB = "digital_store_roles_test"
EXPECTED_DATABASES = ["digital_store_roles_test", "postgres", "template0", "template1"]
SERVER_MAJOR = 18
PGCRYPTO_SCHEMA = "ext_pgcrypto"


class G9Error(Exception):
    pass


def _connect(dbname, read_only=False):
    options = "-c default_transaction_read_only=on" if read_only else None
    conn = psycopg.connect(host=HOST, port=PORT, dbname=dbname, user=os.environ["DB_USER"],
                           password=os.environ.get("DB_PASSWORD"), connect_timeout=10, autocommit=True,
                           application_name="ds-ci-g9-db", options=options)
    if (conn.info.hostaddr, conn.info.port) != (HOST, PORT):
        conn.close()
        raise G9Error(f"подключение не к {HOST}:{PORT}")
    return conn


def _rows(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return [list(row) for row in cur.fetchall()]


def _one(conn, sql, params=None):
    return _rows(conn, sql, params)[0][0]


def _require_github_hosted_job(main_baseline):
    runner_temp = os.environ.get("RUNNER_TEMP", "")
    if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted":
        raise G9Error("только GitHub-hosted runner (GITHUB_ACTIONS, RUNNER_ENVIRONMENT)")
    if not runner_temp or not main_baseline.is_file() or main_baseline.resolve().parent != Path(runner_temp).resolve():
        raise G9Error("нет снимка основной базы этого job в RUNNER_TEMP (prepare_test_db.py не выполнялся)")
    if not os.environ.get("GITHUB_ENV"):
        raise G9Error("нет GITHUB_ENV: пароль роли некуда передать")
    baseline = json.loads(main_baseline.read_text(encoding="utf-8"))
    if baseline.get("database_name") != MAIN_DB:
        raise G9Error(f"снимок основной базы не от {MAIN_DB}")
    return sorted(role[0] for role in baseline["roles"])


def _require_disposable_cluster(conn, baseline_roles):
    """Все условия одноразового кластера этого job; данные не меняются."""
    server = _rows(conn, """
        SELECT current_user, (SELECT rolsuper FROM pg_roles WHERE rolname = current_user),
               inet_server_addr() IS NOT NULL
                   AND NOT (inet_server_addr() <<= inet '127.0.0.0/8' OR inet_server_addr() = inet '::1'),
               inet_server_port(), current_setting('server_version_num')::int / 10000,
               now() - pg_postmaster_start_time() < interval '2 hours'""")[0]
    if server != [os.environ["DB_USER"], True, True, PORT, SERVER_MAJOR, True]:
        raise G9Error(f"не одноразовый сервер этого job (роль, суперпользователь, адрес контейнера, порт, "
                      f"версия, время запуска): {server}")
    databases = sorted(row[0] for row in _rows(conn, "SELECT datname FROM pg_database"))
    if databases != EXPECTED_DATABASES:
        raise G9Error(f"базы кластера {databases}, ожидаются ровно {EXPECTED_DATABASES}")
    roles = sorted(row[0] for row in _rows(conn, "SELECT rolname FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%'"))
    if roles != baseline_roles:
        raise G9Error(f"роли кластера {roles} не совпадают со снимком prepare_test_db.py этого job {baseline_roles}")


def _snapshot(conn):
    """Состояние базы и роли G9, которое тесты не должны менять (соединение — к базе G9)."""
    return {
        "role": _rows(conn, """
            SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication,
                   rolbypassrls, rolconnlimit
            FROM pg_roles WHERE rolname = %s""", (G9_ROLE,)),
        "memberships": _rows(conn, """
            SELECT r.rolname, m.rolname FROM pg_auth_members a JOIN pg_roles r ON r.oid = a.roleid
            JOIN pg_roles m ON m.oid = a.member WHERE r.rolname = %s OR m.rolname = %s ORDER BY 1, 2""",
                             (G9_ROLE, G9_ROLE)),
        "database": _rows(conn, """
            SELECT datname, pg_get_userbyid(datdba), coalesce(datacl, acldefault('d', datdba))::text,
                   shobj_description(oid, 'pg_database'), datconnlimit
            FROM pg_database WHERE datname = current_database()"""),
        "schemas": _rows(conn, """
            SELECT nspname, pg_get_userbyid(nspowner), coalesce(nspacl, acldefault('n', nspowner))::text
            FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema' ORDER BY 1"""),
        "extensions": _rows(conn, """
            SELECT e.extname, n.nspname, pg_get_userbyid(e.extowner) FROM pg_extension e
            JOIN pg_namespace n ON n.oid = e.extnamespace ORDER BY 1"""),
        "cluster_roles": _rows(conn, "SELECT rolname FROM pg_roles WHERE rolname NOT LIKE 'pg\\_%' ORDER BY 1"),
        "cluster_databases": _rows(conn, "SELECT datname FROM pg_database ORDER BY 1"),
    }


def _role_problems(snap):
    expected = [[G9_ROLE, False, True, False, False, True, False, False, -1]]
    problems = [] if snap["role"] == expected else [f"атрибуты роли {G9_ROLE}: {snap['role']}"]
    if snap["memberships"]:
        problems.append(f"у роли {G9_ROLE} есть членства: {snap['memberships']}")
    database = snap["database"]
    if len(database) != 1 or database[0][0] != G9_DB or database[0][3] != MARKER:
        problems.append(f"база {G9_DB} или её маркер: {database}")
    return problems


def prepare(main_baseline, baseline):
    baseline_roles = _require_github_hosted_job(main_baseline)
    if G9_ROLE in baseline_roles:
        raise G9Error(f"роль {G9_ROLE} уже есть в снимке основной базы")
    password = secrets.token_hex(24)                          # [0-9a-f]: безопасно в литерале, нечего экранировать
    print(f"::add-mask::{password}")                          # маска — до любого другого вывода и записи
    sys.stdout.flush()
    admin = _connect("postgres")
    try:
        _require_disposable_cluster(admin, baseline_roles)
        with admin.cursor() as cur:
            cur.execute(f"CREATE ROLE {G9_ROLE} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS "
                        f"INHERIT PASSWORD '{password}'")
            cur.execute(f"CREATE DATABASE {G9_DB}")
            cur.execute(f"COMMENT ON DATABASE {G9_DB} IS '{MARKER}'")
    finally:
        admin.close()
    conn = _connect(G9_DB)
    try:
        with conn.cursor() as cur:
            for statement in (f"REVOKE CONNECT, TEMPORARY ON DATABASE {G9_DB} FROM PUBLIC",
                              f"GRANT CONNECT, CREATE ON DATABASE {G9_DB} TO {G9_ROLE}",
                              "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
                              f"CREATE SCHEMA {PGCRYPTO_SCHEMA}",
                              f"CREATE EXTENSION pgcrypto WITH SCHEMA {PGCRYPTO_SCHEMA}",
                              f"REVOKE ALL ON SCHEMA {PGCRYPTO_SCHEMA} FROM PUBLIC",
                              f"GRANT USAGE ON SCHEMA {PGCRYPTO_SCHEMA} TO {G9_ROLE}"):
                cur.execute(statement)
        snap = _snapshot(conn)
    finally:
        conn.close()
    problems = _role_problems(snap)
    if problems:
        raise G9Error("база или роль G9 после подготовки: " + "; ".join(problems))
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as env_file:
        env_file.write(f"G9_DB_PASSWORD={password}\n")
    del password
    baseline.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"База {G9_DB} и роль {G9_ROLE} готовы: схемы {[s[0] for s in snap['schemas']]}, "
          f"расширения {snap['extensions']}, маркер {MARKER}.")
    return 0


def check(baseline):
    if not baseline.is_file():
        raise G9Error("нет снимка G9 — prepare не выполнялся или не завершился")
    before = json.loads(baseline.read_text(encoding="utf-8"))
    conn = _connect(G9_DB, read_only=True)
    try:
        after = _snapshot(conn)
        leftover = [row[0] for row in _rows(conn, "SELECT nspname FROM pg_namespace WHERE left(nspname, 7) = %s "
                                                  "ORDER BY 1", ("ds_mig_",))]
        g9_sessions = _rows(conn, "SELECT pid, usename, state FROM pg_stat_activity "
                                  "WHERE left(application_name, 6) = %s", ("ds-g9-",))
    finally:
        conn.close()
    problems = _role_problems(after)
    if leftover:
        problems.append(f"остались временные схемы: {leftover}")
    if g9_sessions:
        problems.append(f"остались соединения ds-g9-*: {g9_sessions}")
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            problems.append(f"{key}: было {before.get(key)}, стало {after.get(key)}")
    print(json.dumps({"временные схемы": leftover, "соединения ds-g9": len(g9_sessions),
                      "схемы": [s[0] for s in after["schemas"]]}, ensure_ascii=False))
    if problems:
        for line in problems:
            print(f"НАРУШЕНИЕ: {line}", file=sys.stderr)
        return 1
    print(f"База {G9_DB} после тестов чиста; роль, права и маркер совпадают со снимком.")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--main-baseline", type=Path, required=True)
    prepare_parser.add_argument("--baseline", type=Path, required=True)
    check_parser = commands.add_parser("check")
    check_parser.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "prepare":
        return prepare(args.main_baseline, args.baseline)
    return check(args.baseline)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (G9Error, psycopg.Error) as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        sys.exit(1)
