"""Система SQL-миграций Digital Store.

Использование (из корня проекта, PowerShell):

    .\\.venv\\Scripts\\python.exe migrate.py status --database digital_store_test
    .\\.venv\\Scripts\\python.exe migrate.py apply  --database digital_store_test

Применение к базе, имя которой не оканчивается на "_test" (например, к основной
digital_store), требует повторить её имя в --confirm-database и предварительно
сделать резервную копию:

    .\\.venv\\Scripts\\python.exe migrate.py apply --database digital_store --confirm-database digital_store

Правила:
  * Миграции лежат в migrations/ и называются NNNN_описание.sql (например 0002_order_items.sql).
  * Весь запуск apply — одна транзакция: журнал schema_migrations, все ожидающие миграции,
    их проверки структуры и записи в журнал. Либо применилось всё, либо база не изменилась
    совсем (даже журнал не создаётся, если его не было).
  * BEGIN/COMMIT/ROLLBACK внутри файлов запрещены — транзакцией управляет этот скрипт.
  * Уже применённые файлы менять нельзя: контрольная сумма сверяется при каждом запуске.
    Исправления делаются новой миграцией.
  * Одновременно может работать только один экземпляр (advisory lock PostgreSQL).
"""

import argparse
import hashlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Произвольная константа для pg_try_advisory_xact_lock: "одновременно мигрирует только один процесс".
LOCK_KEY = 7_316_201_001

FILENAME_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
TRANSACTION_CONTROL_RE = re.compile(
    r"^\s*(BEGIN|COMMIT|ROLLBACK|START\s+TRANSACTION|END)\s*;",
    re.IGNORECASE | re.MULTILINE,
)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    applied_by TEXT NOT NULL DEFAULT current_user
)
"""


class MigrationError(Exception):
    """Любая проблема с миграциями. Сообщение предназначено человеку."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    path: Path
    sql: str
    checksum: str


def _normalize(raw: bytes) -> str:
    # Контрольная сумма не должна зависеть от BOM и от того, CRLF или LF
    # (Git на Windows может менять окончания строк).
    text = raw.decode("utf-8-sig")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def discover(migrations_dir: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Читает и проверяет файлы миграций. К базе не обращается."""
    if not migrations_dir.is_dir():
        raise MigrationError(f"Папка миграций не найдена: {migrations_dir}")

    migrations = []
    seen = {}
    for path in sorted(migrations_dir.iterdir()):
        if path.is_dir() or path.name.startswith("."):
            continue
        match = FILENAME_RE.match(path.name)
        if not match:
            raise MigrationError(
                f"Неверное имя файла миграции: {path.name}. Ожидается NNNN_описание.sql "
                "(цифры, строчные латинские буквы и подчёркивания)."
            )
        version, name = match.groups()
        if version in seen:
            raise MigrationError(
                f"Две миграции с номером {version}: {seen[version]} и {path.name}"
            )
        seen[version] = path.name

        sql = _normalize(path.read_bytes())
        if not sql.strip():
            raise MigrationError(f"Пустая миграция: {path.name}")
        bad = TRANSACTION_CONTROL_RE.search(_strip_dollar_quoted_bodies(sql))
        if bad:
            raise MigrationError(
                f"{path.name}: управление транзакцией ({bad.group(1).upper()}) внутри миграции "
                "запрещено — migrate.py сам оборачивает каждую миграцию в транзакцию."
            )
        checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        migrations.append(Migration(version, name, path, sql, checksum))

    return sorted(migrations, key=lambda m: m.version)


def _strip_dollar_quoted_bodies(sql: str) -> str:
    # Внутри DO $$ ... $$ слова BEGIN/END — это PL/pgSQL, а не управление транзакцией.
    return re.sub(r"\$([a-zA-Z_]*)\$.*?\$\1\$", "", sql, flags=re.DOTALL)


def _applied(conn) -> dict[str, tuple[str, str]]:
    """{version: (name, checksum)} или пустой словарь, если таблицы ещё нет. Только чтение."""
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('schema_migrations') IS NOT NULL")
        exists = cur.fetchone()[0]
        if not exists:
            return {}
        cur.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version")
        return {row[0]: (row[1], row[2]) for row in cur.fetchall()}


def _check_consistency(migrations: list[Migration], applied: dict) -> list[str]:
    """Возвращает список проблем: изменённые применённые файлы и неизвестные версии."""
    problems = []
    by_version = {m.version: m for m in migrations}
    for version, (name, checksum) in applied.items():
        migration = by_version.get(version)
        if migration is None:
            problems.append(
                f"В базе отмечена миграция {version}_{name}, но такого файла нет. "
                "База новее кода — обнови код."
            )
        elif migration.checksum != checksum:
            problems.append(
                f"Файл {migration.path.name} изменён после применения "
                "(контрольная сумма не совпадает). Верни исходный текст и сделай новую миграцию."
            )
    return problems


def status(conn, migrations_dir: Path = MIGRATIONS_DIR) -> dict:
    """Состояние миграций. Ничего не записывает в базу."""
    migrations = discover(migrations_dir)
    try:
        applied = _applied(conn)
    finally:
        conn.rollback()
    return {
        "applied": [m for m in migrations if m.version in applied],
        "pending": [m for m in migrations if m.version not in applied],
        "problems": _check_consistency(migrations, applied),
    }


def apply_migrations(conn, migrations_dir: Path = MIGRATIONS_DIR, log=print) -> list[Migration]:
    """Применяет все неприменённые миграции по порядку — ОДНОЙ транзакцией.

    conn — соединение psycopg с autocommit=False. Возвращает список применённых миграций.

    Всё выполняется в одной транзакции: блокировка, создание журнала
    schema_migrations, все ожидающие миграции (включая их проверки структуры)
    и записи в журнал. Если любая миграция или проверка падает, откатывается
    всё — база остаётся ровно в том состоянии, в каком была до запуска,
    в том числе без таблицы schema_migrations, если её раньше не было.
    """
    if conn.autocommit:
        raise MigrationError("Нужно соединение с autocommit=False.")

    migrations = discover(migrations_dir)
    conn.rollback()  # начать с чистой транзакции

    current = None
    try:
        with conn.cursor() as cur:
            # Блокировка уровня транзакции: снимается сама при COMMIT/ROLLBACK.
            cur.execute("SELECT pg_try_advisory_xact_lock(%s)", (LOCK_KEY,))
            if not cur.fetchone()[0]:
                raise MigrationError("Миграции уже выполняются другим процессом. Повтори позже.")

        applied = _applied(conn)
        problems = _check_consistency(migrations, applied)
        if problems:
            raise MigrationError("\n".join(problems))

        pending = [m for m in migrations if m.version not in applied]
        if not pending:
            conn.rollback()
            return []

        with conn.cursor() as cur:
            # Не ждать бесконечно, если таблицу держит работающее приложение.
            cur.execute("SET LOCAL lock_timeout = '10s'")
            cur.execute(CREATE_TABLE_SQL)
            for migration in pending:
                current = migration
                log(f"Проверяю и применяю {migration.path.name} ...")
                cur.execute(migration.sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
        conn.commit()
    except psycopg.Error as exc:
        conn.rollback()
        where = f"Миграция {current.path.name}" if current else "Подготовка миграций"
        raise MigrationError(
            f"{where} не прошла. Все изменения этого запуска откатаны, база не изменена: {exc}"
        ) from exc
    except BaseException:
        conn.rollback()
        raise

    for migration in pending:
        log(f"  готово: {migration.path.name}")
    return pending


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Миграции базы данных Digital Store")
    parser.add_argument("command", choices=["status", "apply"])
    parser.add_argument(
        "--database", required=True,
        help="Имя базы. Указывается явно, чтобы случайно не взять основную базу из .env.",
    )
    parser.add_argument(
        "--confirm-database",
        help="Обязателен для apply к базе, имя которой не оканчивается на _test. Повтори имя базы.",
    )
    args = parser.parse_args(argv)

    if args.command == "apply" and not args.database.endswith("_test"):
        if args.confirm_database != args.database:
            print(
                f"ОТКАЗ: '{args.database}' не тестовая база.\n"
                "Сначала сделай резервную копию (pg_dump -Fc) и проверь её восстановление,\n"
                f"затем повтори команду с --confirm-database {args.database}",
                file=sys.stderr,
            )
            return 2

    # Импорт здесь, чтобы load_dotenv() в database.py не перезаписал явно заданное имя базы.
    os.environ["DB_NAME"] = args.database
    from database import get_connection

    try:
        conn = get_connection()
    except psycopg.Error as exc:
        print(f"Не удалось подключиться к базе '{args.database}': {exc}", file=sys.stderr)
        return 1

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database()")
            actual = cur.fetchone()[0]
        conn.rollback()
        if actual != args.database:
            print(f"ОТКАЗ: подключились к '{actual}', а ожидалась '{args.database}'.", file=sys.stderr)
            return 2

        print(f"База: {actual}")
        if args.command == "status":
            result = status(conn)
            for m in result["applied"]:
                print(f"  [применена]  {m.path.name}")
            for m in result["pending"]:
                print(f"  [ожидает]    {m.path.name}")
            for problem in result["problems"]:
                print(f"  ПРОБЛЕМА: {problem}", file=sys.stderr)
            return 1 if result["problems"] else 0

        done = apply_migrations(conn)
        print(f"Применено миграций: {len(done)}" if done else "Новых миграций нет — база актуальна.")
        return 0
    except MigrationError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
