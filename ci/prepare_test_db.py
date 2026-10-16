"""Подготовка ОДНОРАЗОВОЙ тестовой базы в контейнере PostgreSQL CI — по DB_ROLES.md, штатными средствами.

    python ci/prepare_test_db.py --database digital_store_roles_test --prefix ci_shop --baseline <файл.json>

Порядок (как пошаговый переход DB_ROLES.md, но на пустом кластере):
  1. роли магазина — deploy/db_roles.py create_roles_sql (мигратор БЕЗ SUPERUSER, app, worker, NOLOGIN-группа admin);
  2. база (владелец — суперпользователь контейнера, как у рабочей базы), мигратору — USAGE/CREATE на public;
  3. CONNECT — deploy/db_roles.py database_sql: REVOKE у PUBLIC, GRANT ролям магазина;
  4. миграции 0000–0004 — migrate.py apply ПОД МИГРАТОРОМ (владелец таблиц и SECURITY DEFINER-функций — он);
  5. права и shop_security_settings.admin_group — deploy/db_roles.py grants_sql;
  6. seed.sql — под мигратором;
  7. проверка модели безопасности и снимок для проверки после тестов.

Безопасность:
  * только база *_test на локальном хосте (контейнер CI); иначе отказ;
  * роли не должны существовать заранее (кластер одноразовый) — иначе отказ, ничего не меняется;
  * пароль мигратора случайный, живёт только в этом процессе (передаётся migrate.py через окружение
    дочернего процесса), нигде не печатается и не сохраняется; у app/worker паролей нет (тесты создают
    свои временные роли);
  * суперпользователь — только суперпользователь одноразового контейнера (DB_USER/DB_PASSWORD).
"""

import argparse
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_invariants import ROOT, InvariantError, check_invariants, connect, require_disposable_target, snapshot  # noqa: E402

from deploy import db_roles  # noqa: E402


def _exec(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--baseline", required=True, help="куда записать снимок (JSON, без секретов)")
    args = parser.parse_args(argv)

    host = os.environ.get("DB_HOST", "")
    superuser, super_password = os.environ["DB_USER"], os.environ.get("DB_PASSWORD")
    require_disposable_target(args.database, host)
    names = db_roles.role_names(args.prefix)   # проверка имени префикса — внутри db_roles

    admin_conn = connect("postgres", superuser, super_password, autocommit=True)
    try:
        with admin_conn.cursor() as cur:
            cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            if not cur.fetchone()[0]:
                raise InvariantError("подготовка выполняется суперпользователем одноразового контейнера")
            cur.execute("SELECT current_setting('server_version_num')::int, current_setting('server_version')")
            version_num, version = cur.fetchone()
            cur.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(names.values()),))
            existing = [r[0] for r in cur.fetchall()]
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (args.database,))
            db_exists = cur.fetchone() is not None
        if version_num < 160000:
            raise InvariantError(f"нужен PostgreSQL 16+ (GRANT ... WITH INHERIT/SET), сервер {version}")
        if existing or db_exists:
            raise InvariantError(f"кластер не пустой (роли {existing}, база существует: {db_exists}) — ожидается "
                                 "одноразовый контейнер CI")
        print(f"PostgreSQL {version}")

        # 1. роли
        _exec(admin_conn, db_roles.create_roles_sql(args.prefix))
        migrator_password = secrets.token_hex(24)          # [0-9a-f], безопасно вставлять в литерал
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(f"::add-mask::{migrator_password}")      # на случай случайного вывода; само значение — маска
        _exec(admin_conn, f"ALTER ROLE {names['migrator']} PASSWORD '{migrator_password}'")
        # 2. база
        _exec(admin_conn, f"CREATE DATABASE {args.database}")
    finally:
        admin_conn.close()

    conn = connect(args.database, superuser, super_password, autocommit=True)
    try:
        _exec(conn, f"GRANT USAGE, CREATE ON SCHEMA public TO {names['migrator']}")
        # 3. CONNECT: REVOKE у PUBLIC, GRANT ролям магазина (штатный текст db_roles.py)
        _exec(conn, db_roles.database_sql(args.database, args.prefix))
    finally:
        conn.close()

    # 4. миграции — штатный migrate.py под мигратором (база *_test: подтверждение имени не требуется)
    env = dict(os.environ, DB_USER=names["migrator"], DB_PASSWORD=migrator_password, DB_NAME=args.database)
    result = subprocess.run([sys.executable, "migrate.py", "apply", "--database", args.database], cwd=ROOT, env=env,
                            capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    if result.returncode != 0:
        sys.stderr.write(result.stderr)
        raise InvariantError("migrate.py apply завершился с ошибкой")

    conn = connect(args.database, superuser, super_password, autocommit=True)
    try:
        # 5. права и группа администраторов (штатный текст db_roles.py; включает shop_security_settings)
        _exec(conn, db_roles.grants_sql("public", args.prefix))
    finally:
        conn.close()

    # 6. начальные данные — под мигратором (владелец таблиц)
    conn = connect(args.database, names["migrator"], migrator_password)
    try:
        _exec(conn, (ROOT / "seed.sql").read_text(encoding="utf-8-sig"))
        conn.commit()
    finally:
        conn.close()
    del migrator_password

    # 7. проверка и снимок
    conn = connect(args.database, superuser, super_password)
    try:
        problems = check_invariants(conn, args.prefix)
        snap = snapshot(conn)
        conn.rollback()
    finally:
        conn.close()
    if problems:
        raise InvariantError("модель безопасности после подготовки нарушена:\n  " + "\n  ".join(problems))
    Path(args.baseline).write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"База {args.database} готова: роли {sorted(names.values())}, миграции "
          f"{[v for v, _ in snap['migrations']]}, admin_group {snap['admin_group']}, CONNECT у PUBLIC отозван.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except InvariantError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        sys.exit(1)
