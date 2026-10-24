"""Очистка устаревших данных авторизации (PR6 Stage 2f-5): отдельный процесс под ролью worker.

    python -m accounts.maintenance --once           # один проход; для эксплуатации — внешний планировщик раз в сутки
    python -m accounts.maintenance --interval N     # постоянно: проход каждые N секунд (60…86400)

Процесс только вызывает существующую функцию базы auth_maintenance_purge() (миграция 0005; EXECUTE — у роли
worker, deploy/db_roles.py): удаление счётчиков попыток входа с истёкшим окном и сессий, истёкших или отозванных
более суток назад; не больше 10 000 строк каждой категории за вызов. Заказы, платежи, ключи и аудит функция не
трогает. В платёжный обработчик и worker выдачи очистка намеренно не встроена: её ошибка не должна останавливать
платежи и выдачу ключей.

Пакет = одно соединение и одна короткая транзакция (psycopg 3: BEGIN перед первым запросом, COMMIT при выходе
из `with conn`, ROLLBACK при исключении):
  1. set_config(..., true) — statement_timeout 1500 мс и lock_timeout 500 мс только для ЭТОЙ транзакции;
  2. SELECT auth_maintenance_purge();
  3. строгая проверка ответа: ровно {"throttle_deleted", "sessions_deleted"}, целые 0…10000; иначе ROLLBACK;
  4. COMMIT.
Функция отбирает строки через FOR UPDATE SKIP LOCKED (занятые строки не ждёт), но удалённые держит до COMMIT —
короткие пакеты и тайм-ауты не дают ей надолго задержать вход и регистрацию. Пакеты повторяются, пока хотя бы
одна категория удалила 10 000 строк, но не больше 5 за проход. Параллельные экземпляры безопасны (SKIP LOCKED).

Ошибки: --once — запись в журнал и код 1; --interval — запись в журнал, ожидание следующего периода и повтор
(интервал не меньше 60 с: недоступная база не превращается в быстрый цикл). Неизвестный исход COMMIT
компенсации не требует: очистка идемпотентна, следующий проход повторит её.

Журнал — только длительность, число пакетов, число удалённых строк, тип исключения и SQLSTATE: без DSN,
паролей, email, токенов, параметров SQL и текстов сообщений PostgreSQL.

Секреты аккаунтов процессу не нужны: если они есть в окружении, он отказывается запускаться. Модуль не
импортирует database.py (он читает .env при импорте), payments, commerce и cors_settings: подключение
создаётся только при реальном проходе.
"""

import argparse
import logging
import os
import sys
import time

from accounts.config import ENV_SECRETS_FILE, SECRET_NAMES

SQL_LOCAL_LIMITS = "SELECT set_config('statement_timeout', %s, true), set_config('lock_timeout', %s, true)"
SQL_PURGE = "SELECT auth_maintenance_purge()"
RESULT_KEYS = ("throttle_deleted", "sessions_deleted")
PURGE_LIMIT = 10_000                 # LIMIT каждой категории в auth_maintenance_purge (миграция 0005)
STATEMENT_TIMEOUT_MS = 1500
LOCK_TIMEOUT_MS = 500
MAX_BATCHES = 5
MIN_INTERVAL_SECONDS = 60
MAX_INTERVAL_SECONDS = 86_400
FORBIDDEN_VARIABLES = tuple(SECRET_NAMES) + (ENV_SECRETS_FILE,)
EXIT_OK, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2

_log = logging.getLogger("digital_store.accounts.maintenance")


class MaintenanceError(Exception):
    """Нарушение контракта очистки. Сообщение не содержит значений из базы."""


class CommitUnknown(Exception):
    """Ошибка при COMMIT: удалены ли строки, неизвестно. Причина — в __cause__."""


class ConfigError(Exception):
    pass


def check_no_secrets(env=None):
    env = os.environ if env is None else env
    present = [name for name in FORBIDDEN_VARIABLES if env.get(name)]
    if present:
        raise ConfigError(f"очистке секреты аккаунтов не нужны; уберите из окружения процесса: {present}")


def _default_connect():
    # Импорт здесь: database.py при импорте читает .env (load_dotenv) — только при реальном проходе.
    from database import get_connection
    return get_connection()


def _counts(value):
    if not isinstance(value, dict) or set(value) != set(RESULT_KEYS):
        raise MaintenanceError("ответ auth_maintenance_purge вне контракта")
    for key in RESULT_KEYS:
        if type(value[key]) is not int or not 0 <= value[key] <= PURGE_LIMIT:
            raise MaintenanceError("ответ auth_maintenance_purge вне контракта")
    return {key: value[key] for key in RESULT_KEYS}


def purge_batch(connect, *, statement_timeout_ms=STATEMENT_TIMEOUT_MS, lock_timeout_ms=LOCK_TIMEOUT_MS):
    """Один пакет: своё соединение, одна транзакция. -> {"throttle_deleted": n, "sessions_deleted": m}."""
    conn = connect()
    try:
        if conn.autocommit:
            raise MaintenanceError("соединение в режиме autocommit: локальные тайм-ауты не подействуют")
        committing = False
        try:
            with conn:                               # COMMIT при выходе, ROLLBACK при исключении
                with conn.cursor() as cursor:
                    cursor.execute(SQL_LOCAL_LIMITS, (f"{statement_timeout_ms}ms", f"{lock_timeout_ms}ms"))
                    cursor.execute(SQL_PURGE)
                    row = cursor.fetchone()
                counts = _counts(row[0] if row else None)
                committing = True
        except Exception as error:
            if committing:
                raise CommitUnknown() from error
            raise
        return counts
    finally:
        conn.close()                                 # psycopg не закрывает соединение, если упал COMMIT в __exit__


def _describe(error):
    cause = error.__cause__ if isinstance(error, CommitUnknown) and error.__cause__ is not None else error
    text = f"{type(cause).__name__}, SQLSTATE {getattr(cause, 'sqlstate', None) or '-'}"
    if isinstance(error, CommitUnknown):
        return f"исход COMMIT неизвестен ({text}); следующий проход повторит очистку"
    return text


def run_pass(connect, *, max_batches=MAX_BATCHES, statement_timeout_ms=STATEMENT_TIMEOUT_MS,
             lock_timeout_ms=LOCK_TIMEOUT_MS, clock=time.monotonic):
    """Проход: пакеты, пока хотя бы одна категория удалила PURGE_LIMIT строк, не больше max_batches.
    -> True — проход завершён без ошибок."""
    started = clock()
    totals = dict.fromkeys(RESULT_KEYS, 0)
    batches, drained = 0, False
    try:
        while batches < max_batches:
            counts = purge_batch(connect, statement_timeout_ms=statement_timeout_ms, lock_timeout_ms=lock_timeout_ms)
            batches += 1
            for key in RESULT_KEYS:
                totals[key] += counts[key]
            if all(counts[key] < PURGE_LIMIT for key in RESULT_KEYS):
                drained = True
                break
    except Exception as error:                       # noqa: BLE001 — очистка некритична; подробности — только тип
        _log.error("очистка прервана: %s; зафиксировано пакетов %d, удалено счётчиков %d, сессий %d, %.3f с",
                   _describe(error), batches, totals["throttle_deleted"], totals["sessions_deleted"],
                   clock() - started)
        return False
    _log.info("очистка: пакетов %d, удалено счётчиков %d, сессий %d, %.3f с%s", batches, totals["throttle_deleted"],
              totals["sessions_deleted"], clock() - started,
              "" if drained else "; достигнут предел пакетов — остаток удалит следующий проход")
    return True


def _interval(value):
    try:
        seconds = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("ожидается целое число секунд") from None
    if not MIN_INTERVAL_SECONDS <= seconds <= MAX_INTERVAL_SECONDS:
        raise argparse.ArgumentTypeError(f"от {MIN_INTERVAL_SECONDS} до {MAX_INTERVAL_SECONDS} секунд")
    return seconds


def main(argv=None, *, env=None, connect=None, sleep=time.sleep):
    parser = argparse.ArgumentParser(description="Очистка устаревших данных авторизации Digital Store")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="один проход и выход (для внешнего планировщика)")
    group.add_argument("--interval", type=_interval,
                       help=f"пауза между проходами, секунд ({MIN_INTERVAL_SECONDS}…{MAX_INTERVAL_SECONDS})")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        check_no_secrets(env)
    except ConfigError as error:
        print(f"Ошибка настройки: {error}", file=sys.stderr)
        return EXIT_CONFIG
    connect = connect or _default_connect
    if args.once:
        return EXIT_OK if run_pass(connect) else EXIT_FAILED
    while True:
        run_pass(connect)                            # ошибка — в журнале; следующий проход через полный интервал
        sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
