"""PR6 Stage 2f-5a: accounts/maintenance.py — отдельный процесс очистки данных авторизации. Офлайн, без базы.

Поддельное соединение повторяет семантику psycopg 3.3.6, на которую опирается модуль (проверяется здесь же по
установленному psycopg): BEGIN перед первым запросом без autocommit; `with conn` — COMMIT при выходе и ROLLBACK
при исключении, затем close; если COMMIT в __exit__ упал, psycopg соединение НЕ закрывает. Контракт ответа
сверяется с текстом миграции 0005; ответ функции — настоящим загрузчиком JSONB psycopg.
"""

import ast
import inspect
import logging
import re
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from psycopg.adapt import Transformer
from psycopg.pq import Format

from accounts import maintenance
from accounts.maintenance import (
    LOCK_TIMEOUT_MS, MAX_BATCHES, PURGE_LIMIT, RESULT_KEYS, SQL_LOCAL_LIMITS, SQL_PURGE, STATEMENT_TIMEOUT_MS,
    main, run_pass,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
LOGGER = "digital_store.accounts.maintenance"
LEAKY = "host=db.internal password=hunter2 user=buyer@example.test token=AbC_secret"   # не должно попасть в журнал


# ---------- поддельное соединение с семантикой psycopg 3 ----------

class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        if self.conn.closed:
            raise psycopg.InterfaceError("the connection is closed")
        if not self.conn.in_transaction:
            self.conn.in_transaction = True
            self.conn.log.append("BEGIN")
        self.conn.log.append(("SQL", sql, params))
        if sql == SQL_PURGE:
            if isinstance(self.conn.answer, BaseException):
                raise self.conn.answer
            self.last = self.conn.answer

    def fetchone(self):
        return self.last


class _Connection:
    def __init__(self, answer, *, commit_error=None, autocommit=False):
        self.answer, self.commit_error, self.autocommit = answer, commit_error, autocommit
        self.closed, self.in_transaction, self.log = False, False, []

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        if self.commit_error is not None:
            raise self.commit_error
        self.log.append("COMMIT")
        self.in_transaction = False

    def rollback(self):
        self.log.append("ROLLBACK")
        self.in_transaction = False

    def close(self):
        if not self.closed:
            self.log.append("CLOSE")
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):                # как psycopg 3.3.6 Connection.__exit__
        if self.closed:
            return
        if exc_type:
            self.rollback()
        else:
            self.commit()                                  # исключение здесь — close() ниже не выполняется
        self.close()


class _FastLoop(BaseException):
    """Повтор без паузы: тест падает сразу, а не зависает (run_pass ловит только Exception)."""


class _CommitFails:
    """Ответ функции получен, но COMMIT падает с error."""

    def __init__(self, row, error):
        self.row, self.error = row, error


class _Lab:
    """connect(): соединения по очереди из answers — строка ответа функции, исключение подключения или
    _CommitFails."""

    def __init__(self, *answers):
        self.answers, self.opened = list(answers), []

    def connect(self):
        if not self.answers:
            raise _FastLoop("ответы кончились: лишние попытки подключения")   # BaseException — run_pass не поймает
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        commit_error = None
        if isinstance(answer, _CommitFails):
            answer, commit_error = answer.row, answer.error
        conn = _Connection(answer, commit_error=commit_error)
        self.opened.append(conn)
        return conn


def _row(throttle, sessions):
    return ({"throttle_deleted": throttle, "sessions_deleted": sessions},)


BATCH_LOG = ["BEGIN", ("SQL", SQL_LOCAL_LIMITS, ("1500ms", "500ms")), ("SQL", SQL_PURGE, None), "COMMIT", "CLOSE"]


# ---------- пакет и проход ----------

def test_single_batch_commits_one_transaction_and_logs_counts(caplog):
    lab = _Lab(_row(3, 2))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert run_pass(lab.connect) is True
    [conn] = lab.opened
    assert conn.log == BATCH_LOG and conn.closed                     # одна транзакция: BEGIN … COMMIT, затем close
    assert "пакетов 1, удалено счётчиков 3, сессий 2" in caplog.text and "предел пакетов" not in caplog.text


def test_local_timeouts_are_set_inside_the_transaction_before_the_purge():
    lab = _Lab(_row(0, 0))
    run_pass(lab.connect)
    log = lab.opened[0].log
    assert log.index("BEGIN") < log.index(("SQL", SQL_LOCAL_LIMITS, ("1500ms", "500ms"))) \
        < log.index(("SQL", SQL_PURGE, None)) < log.index("COMMIT")
    # set_config(..., true) = SET LOCAL: действует до конца транзакции, не на соединение и не на сервер.
    assert re.fullmatch(r"SELECT set_config\('statement_timeout', %s, true\), set_config\('lock_timeout', %s, true\)",
                        SQL_LOCAL_LIMITS)
    assert (STATEMENT_TIMEOUT_MS, LOCK_TIMEOUT_MS, MAX_BATCHES) == (1500, 500, 5)


def test_autocommit_connection_is_refused_before_any_sql(caplog):
    conn = _Connection(_row(0, 0), autocommit=True)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert run_pass(lambda: conn) is False
    assert conn.log == ["CLOSE"]                                        # локальные тайм-ауты не подействовали бы
    assert "MaintenanceError" in caplog.text


def test_repeats_while_a_category_hits_the_limit_and_stops_when_both_are_below():
    lab = _Lab(_row(PURGE_LIMIT, 0), _row(0, PURGE_LIMIT), _row(PURGE_LIMIT - 1, PURGE_LIMIT - 1), _row(7, 7))
    assert run_pass(lab.connect) is True
    assert len(lab.opened) == 3 and all(conn.log == BATCH_LOG for conn in lab.opened)   # третий пакет < 10 000
    assert lab.answers == [_row(7, 7)]                                  # четвёртого соединения не было


def test_batches_are_capped_at_max_batches(caplog):
    lab = _Lab(*[_row(PURGE_LIMIT, PURGE_LIMIT)] * 8)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert run_pass(lab.connect) is True
    assert len(lab.opened) == MAX_BATCHES == 5
    assert "пакетов 5, удалено счётчиков 50000, сессий 50000" in caplog.text
    assert "достигнут предел пакетов" in caplog.text


@pytest.mark.parametrize("row", [
    None, (None,), ({},), ({"throttle_deleted": 1},), ({"throttle_deleted": 1, "sessions_deleted": 1, "x": 0},),
    _row(-1, 0), _row(0, PURGE_LIMIT + 1), _row(True, 0), _row(1.0, 0), _row("1", 0), ([1, 2],), ("ok",),
], ids=["нет строки", "null", "пусто", "нет ключа", "лишний ключ", "отрицательное", "больше предела", "bool",
        "float", "строка", "список", "текст"])
def test_answer_outside_the_contract_is_rolled_back(caplog, row):
    lab = _Lab(row)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert run_pass(lab.connect) is False
    assert lab.opened[0].log == BATCH_LOG[:3] + ["ROLLBACK", "CLOSE"]  # без COMMIT
    assert "MaintenanceError" in caplog.text


@pytest.mark.parametrize("error, sqlstate", [
    (psycopg.errors.QueryCanceled(LEAKY), "57014"),                    # statement_timeout
    (psycopg.errors.LockNotAvailable(LEAKY), "55P03"),                 # lock_timeout
    (psycopg.errors.RaiseException(LEAKY), "P0001"),
    (psycopg.errors.InsufficientPrivilege(LEAKY), "42501"),            # запуск не под ролью worker
    (RuntimeError(LEAKY), "-"),
])
def test_sql_error_rolls_back_and_logs_only_type_and_sqlstate(caplog, error, sqlstate):
    conn = _Connection(error)                                           # ошибка при SELECT auth_maintenance_purge()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert run_pass(lambda: conn) is False
    assert conn.log == BATCH_LOG[:3] + ["ROLLBACK", "CLOSE"]
    assert f"{type(error).__name__}, SQLSTATE {sqlstate}" in caplog.text
    _assert_safe(caplog.text)


def test_unavailable_database_is_logged_safely(caplog):
    lab = _Lab(psycopg.OperationalError(LEAKY))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert run_pass(lab.connect) is False
    assert "OperationalError" in caplog.text and lab.opened == []
    _assert_safe(caplog.text)


def test_commit_failure_is_an_unknown_outcome_and_the_connection_is_still_closed(caplog):
    lab = _Lab(_CommitFails(_row(5, 5), psycopg.OperationalError(LEAKY)))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert run_pass(lab.connect) is False
    [conn] = lab.opened
    assert "COMMIT" not in conn.log and conn.closed                  # psycopg сам не закрыл бы — закрыл модуль
    assert "исход COMMIT неизвестен (OperationalError" in caplog.text and "удалено счётчиков 0" in caplog.text
    _assert_safe(caplog.text)


def test_error_in_a_later_batch_keeps_earlier_commits_and_stops(caplog):
    lab = _Lab(_row(PURGE_LIMIT, 1), psycopg.OperationalError(LEAKY), _row(1, 1))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert run_pass(lab.connect) is False
    assert len(lab.opened) == 1 and lab.answers == [_row(1, 1)]       # после ошибки проход остановлен
    assert "зафиксировано пакетов 1, удалено счётчиков 10000, сессий 1" in caplog.text


def _assert_safe(text):
    for leaked in ("hunter2", "password", "buyer@example.test", "AbC_secret", "db.internal", "host=", "token="):
        assert leaked not in text, leaked


# ---------- запуск: --once, --interval, секреты ----------

class _Stop(BaseException):
    """Тест останавливает бесконечный цикл --interval через подменённый sleep."""


def _sleeper(calls, limit):
    def sleep(seconds):
        calls.append(seconds)
        if len(calls) >= limit:
            raise _Stop
    return sleep


def test_once_returns_0_on_success_and_1_on_failure():
    assert main(["--once"], env={}, connect=_Lab(_row(1, 1)).connect) == 0
    assert main(["--once"], env={}, connect=_Lab(psycopg.OperationalError(LEAKY)).connect) == 1
    assert main(["--once"], env={}, connect=_Lab(_CommitFails(_row(1, 1), psycopg.OperationalError("x"))).connect) == 1


def test_interval_waits_the_full_period_after_success_and_after_errors():
    lab = _Lab(_row(1, 1), psycopg.OperationalError(LEAKY), psycopg.OperationalError(LEAKY), _row(0, 0))
    calls = []
    with pytest.raises(_Stop):
        main(["--interval", "600"], env={}, connect=lab.connect, sleep=_sleeper(calls, 4))
    assert calls == [600, 600, 600, 600] and lab.answers == []        # один проход на период, без быстрых повторов


def test_unavailable_database_never_becomes_a_fast_loop():
    attempts, calls = [], []

    def connect():
        attempts.append(1)
        if len(attempts) > len(calls) + 1:
            raise _FastLoop("повторное подключение без паузы")      # BaseException — run_pass не поймает
        raise psycopg.OperationalError(LEAKY)

    with pytest.raises(_Stop):
        main(["--interval", "60"], env={}, connect=connect, sleep=_sleeper(calls, 3))
    assert len(attempts) == 3 and calls == [60, 60, 60]               # одна попытка на период, пауза каждый раз


@pytest.mark.parametrize("argv", [[], ["--once", "--interval", "60"], ["--interval", "0"], ["--interval", "-60"],
                                  ["--interval", "59"], ["--interval", "86401"], ["--interval", "1.5"],
                                  ["--interval", "abc"], ["--interval"]])
def test_invalid_arguments_are_refused_before_any_connection(argv):
    def connect():
        pytest.fail("соединение при неверных аргументах")
    with pytest.raises(SystemExit) as exit_info:
        main(argv, env={}, connect=connect)
    assert exit_info.value.code == 2


@pytest.mark.parametrize("seconds", ["60", "86400"])
def test_interval_bounds_are_inclusive(seconds):
    calls = []
    with pytest.raises(_Stop):
        main(["--interval", seconds], env={}, connect=_Lab(_row(0, 0)).connect, sleep=_sleeper(calls, 1))
    assert calls == [int(seconds)]


@pytest.mark.parametrize("name", ["ACCOUNT_PEPPER_CURRENT", "ACCOUNT_PEPPER_PREVIOUS", "ACCOUNT_CSRF_KEY",
                                  "DS_ACCOUNT_SECRETS_FILE"])
def test_account_secrets_in_the_environment_are_refused(capsys, name):
    def connect():
        pytest.fail("соединение при секретах в окружении")
    assert main(["--once"], env={name: "secret-value-123"}, connect=connect) == 2
    error = capsys.readouterr().err
    assert name in error and "secret-value-123" not in error


def test_empty_secret_variables_and_non_secret_account_settings_are_allowed():
    env = {"ACCOUNT_PEPPER_CURRENT": "", "DS_ACCOUNT_SECRETS_FILE": "", "ACCOUNT_TRUSTED_ORIGINS": "https://shop.test",
           "CORS_ALLOWED_ORIGINS": "", "DS_ACCOUNTS_ENABLED": "on", "TRUSTED_PROXY_IPS": "10.0.0.1"}
    assert main(["--once"], env=env, connect=_Lab(_row(0, 0)).connect) == 0


# ---------- изоляция: импорты, .env, платежи и выдача ключей ----------

def test_module_level_imports_are_only_stdlib_and_account_config():
    tree = ast.parse((PROJECT_DIR / "accounts" / "maintenance.py").read_text(encoding="utf-8"))
    top = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            top.add(node.module)
    assert top == {"argparse", "logging", "os", "sys", "time", "accounts.config"}


def test_import_and_help_read_no_env_file_and_load_no_shop_modules():
    probe = r'''
import sys
sys.path.insert(0, sys.argv[1])
import dotenv, dotenv.main, psycopg

def _forbidden(*args, **kwargs):
    raise AssertionError("запрещено при импорте и --help")

dotenv.load_dotenv = dotenv.main.load_dotenv = dotenv.find_dotenv = dotenv.main.find_dotenv = _forbidden
dotenv.dotenv_values = dotenv.main.dotenv_values = _forbidden
psycopg.connect = _forbidden
import accounts.maintenance
loaded = [m for m in ("database", "cors_settings", "payments", "commerce", "api", "http_stack") if m in sys.modules]
print("loaded:", loaded)
try:
    accounts.maintenance.main(["--help"])
except SystemExit as exc:
    print("help-exit:", exc.code)
'''
    done = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", probe, str(PROJECT_DIR)], capture_output=True,
                          text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    assert "loaded: []" in done.stdout and "help-exit: 0" in done.stdout


def test_payment_and_fulfillment_workers_are_untouched_and_unrelated():
    source = (PROJECT_DIR / "accounts" / "maintenance.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.split('"""')[2::2])        # код вне строк документации модуля
    for word in ("payments", "commerce", "reconcile", "fulfillment", "digital_keys"):
        assert word not in code, word
    for worker in ("payments/worker.py", "commerce/fulfillment_worker.py"):
        text = (PROJECT_DIR / worker).read_text(encoding="utf-8")
        assert "maintenance" not in text and "auth_maintenance_purge" not in text, worker


def test_only_the_purge_function_is_called():
    lab = _Lab(_row(0, 0))
    run_pass(lab.connect)
    statements = [entry[1] for entry in lab.opened[0].log if isinstance(entry, tuple)]
    assert statements == [SQL_LOCAL_LIMITS, SQL_PURGE]


# ---------- сверка с настоящими источниками: миграция 0005, psycopg 3, права ролей ----------

def test_contract_matches_the_migration():
    sql = (PROJECT_DIR / "migrations" / "0005_customer_auth.sql").read_text(encoding="utf-8-sig")
    body = sql[sql.index("CREATE FUNCTION auth_maintenance_purge()"):]
    body = body[:body.index("$$;")]
    assert body.startswith("CREATE FUNCTION auth_maintenance_purge() RETURNS JSONB")
    assert "SECURITY DEFINER" in body and "FOR UPDATE SKIP LOCKED" in body
    assert body.count(f"LIMIT {PURGE_LIMIT}") == 2                    # предел каждой категории
    assert re.findall(r"jsonb_build_object\('(\w+)', v_throttle, '(\w+)', v_sessions\)", body) == [RESULT_KEYS]
    assert SQL_PURGE == "SELECT auth_maintenance_purge()"


@pytest.mark.parametrize("fmt, data", [(Format.TEXT, b'{"sessions_deleted": 0, "throttle_deleted": 10000}'),
                                       (Format.BINARY, b'\x01{"sessions_deleted": 0, "throttle_deleted": 10000}')])
def test_real_psycopg_loads_the_jsonb_answer_as_accepted_ints(fmt, data):
    value = Transformer().get_loader(psycopg.postgres.types["jsonb"].oid, fmt).load(data)
    assert maintenance._counts(value) == {"throttle_deleted": PURGE_LIMIT, "sessions_deleted": 0}


def test_installed_psycopg_has_the_transaction_semantics_the_module_relies_on():
    """Офлайн по исходникам установленного psycopg; на настоящем сервере — Stage 2f-5b."""
    exit_source = inspect.getsource(psycopg.Connection.__exit__)
    assert exit_source.index("self.rollback()") < exit_source.index("self.commit()") < exit_source.index("self.close()")
    start = inspect.getsource(psycopg.Connection._start_query)
    assert "if self._autocommit:" in start and "_get_tx_start_command()" in start


def test_only_the_worker_role_may_execute_the_purge():
    from deploy import db_roles
    assert "auth_maintenance_purge()" in db_roles.WORKER_FUNCTIONS
    assert not any("auth_maintenance_purge" in f for f in db_roles.APP_FUNCTIONS + db_roles.ADMIN_FUNCTIONS)
