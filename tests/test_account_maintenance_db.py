"""PR6 Stage 2f-5b: очистка данных авторизации на настоящем PostgreSQL — функция auth_maintenance_purge()
(миграция 0005) и процесс accounts.maintenance (run_pass, purge_batch, main --once).

Безопасность запуска (решение владельцев):
  * только явно заданное окружение: PYTHON_DOTENV_DISABLED=1, TEST_DB_NAME, DB_HOST/DB_PORT/DB_USER и пароль из
    скрытого ввода; без них модуль пропускается ДО импорта database.py;
  * защита tests/conftest.py (только TEST_DB_NAME) и _lab_role_guard (test_account_http_db.py): в
    digital_store_pr6_stage1_test — только роль pr6_stage1_owner без прав суперпользователя;
  * всё — во временной схеме ds_mig_<10 hex> на тест (make_schema/at_0005/accounts из test_migration_0005.py);
    схема удаляется в teardown make_schema. Каждое соединение — фабрика _OrderLab (test_account_order_db.py):
    search_path только из временной схемы, до первого SQL проверяются current_database() и
    current_schemas(false) == [схема]; lock_timeout 5 с, statement_timeout 30 с,
    idle_in_transaction_session_timeout 60 с — параметры только этих сеансов;
  * ролей, расширений, баз и глобальных настроек тесты не создают и не меняют; права роли worker — тесты CI
    (test_customer_auth_privileges.py); чужие процессы не завершаются;
  * записи создаются только во временной схеме: покупатели (bcrypt cost 4), сессии, счётчики попыток, товар и
    заказ; объём — не больше 10 001 счётчика на тест; LOCK TABLE и FOR UPDATE — только на таблицах временной
    схемы и только в транзакциях собственных соединений теста.
"""

import os

import pytest

# Значения, при которых python-dotenv 1.2 не читает .env (dotenv.main._load_dotenv_disabled).
_DOTENV_DISABLED_VALUES = {"1", "true", "t", "yes", "y"}

# Проверки — до импорта модулей, которые импортируют database.py (он вызывает load_dotenv() при импорте).
if os.environ.get("PYTHON_DOTENV_DISABLED", "").casefold() not in _DOTENV_DISABLED_VALUES:
    pytest.skip("очистка на PostgreSQL: запуск только с PYTHON_DOTENV_DISABLED=1 (без чтения .env)",
                allow_module_level=True)
if "TEST_DB_NAME" not in os.environ:
    pytest.skip("очистка на PostgreSQL: задайте TEST_DB_NAME явно", allow_module_level=True)

import logging  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402

import database  # noqa: E402
from accounts import maintenance  # noqa: E402
from test_account_http_db import SCHEMA_RE, _lab_role_guard  # noqa: E402,F401  (_lab_role_guard — фикстура)
from test_account_order_db import SESSION_LIMITS, _OrderLab  # noqa: E402
from test_migration_0005 import (  # noqa: E402,F401  (фикстуры)
    EMAIL, _Background, _call, _exec, _link, _new_order, _register, accounts, at_0005, make_schema, product,
)
from test_migrations import _query  # noqa: E402

OTHER = "other@example.test"
LOGGER = "digital_store.accounts.maintenance"
LIMIT = maintenance.PURGE_LIMIT                                  # 10 000 — LIMIT каждой категории в функции
KEPT_TABLES = ("customers", "customer_orders", "orders", "order_items", "order_access", "payments",
               "money_operations", "fulfillments", "audit_log", "auth_settings", "products")
INSIDE = "SELECT current_setting('statement_timeout'), current_setting('lock_timeout'), pg_current_xact_id()::text"


# ---------- стенд ----------

@pytest.fixture
def lab(accounts):
    """Фабрика соединений временной схемы; наблюдатель — соединение владельца схемы (make_schema)."""
    owner, schema = accounts
    assert SCHEMA_RE.fullmatch(schema), "работа только во временной схеме make_schema"
    with owner.cursor() as cur:
        for statement in SESSION_LIMITS:
            cur.execute(statement)
        cur.execute("SELECT current_database(), current_schemas(false)")
        assert cur.fetchone() == (os.environ["TEST_DB_NAME"], [schema])          # до первой записи теста
    owner.commit()
    lab = _OrderLab(owner, schema, database.get_connection)
    try:
        yield lab
    finally:
        lab.close_all()                                          # до удаления схемы (make_schema)


def _write(conn, sql, params=None, *, rows):
    """Запись тестовых данных: ровно rows строк, иначе ошибка подготовки (INSERT … SELECT без исходной строки
    молча вставляет 0 строк — так в первом прогоне 2f-5b «пропали» сессии, которых не было)."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        written = cur.rowcount
    if written != rows:
        conn.rollback()
        raise AssertionError(f"подготовка данных: записано {written} строк вместо {rows}")
    conn.commit()


def _one(conn, sql, params=None):
    return _query(conn, sql, params)[0][0]


def _counters(owner, *, expired=0, live=0):
    """Счётчики попыток: истёкшее окно (час назад, окно 15 минут) и действующее (сейчас). -> метка набора."""
    tag = uuid.uuid4().hex
    for kind, count, start in (("expired", expired, "now() - INTERVAL '1 hour'"), ("live", live, "now()")):
        if count:
            _write(owner, "INSERT INTO auth_throttle (scope, key_sha256, window_start, attempts) "
                          f"SELECT 2, sha256(convert_to(%s || g::text, 'UTF8')), {start}, 1 "
                          "FROM generate_series(1, %s) g", (f"{tag}:{kind}:", count), rows=count)
    return tag


def _expired_counters(owner):
    return _one(owner, "SELECT count(*) FROM auth_throttle WHERE window_start < now() - INTERVAL '15 minutes'")


def _session(owner, email, *, created, idle, expires, revoked=None):
    """Сессия покупателя email (он должен быть зарегистрирован в этой схеме) с заданными сроками (интервалы от
    now()). -> SHA-256 токена (как его хранит база)."""
    token = os.urandom(32)
    _write(owner, "INSERT INTO customer_sessions (customer_id, token_sha256, auth_epoch, created_at, idle_expires_at, "
                  "expires_at, revoked_at) SELECT id, %s, auth_epoch, now() + %s::interval, now() + %s::interval, "
                  "now() + %s::interval, now() + %s::interval FROM customers WHERE email_norm = %s",
           (token, created, idle, expires, revoked, email), rows=1)
    return token


def _old_and_recent_sessions(owner, email=EMAIL):
    """-> (подлежат удалению, остаются). Порог функции — истекли или отозваны более суток назад."""
    old = [_session(owner, email, created="-100 days", idle="-10 days", expires="-2 days"),          # абсолютный срок
           _session(owner, email, created="-40 days", idle="-2 days", expires="50 days"),            # простой
           _session(owner, email, created="-3 days", idle="20 days", expires="80 days", revoked="-2 days")]
    recent = [_session(owner, email, created="-2 days", idle="-1 hour", expires="-1 hour"),           # истекла недавно
              _session(owner, email, created="-1 day", idle="29 days", expires="89 days", revoked="-1 hour")]
    return old, recent


def _sessions_present(owner, tokens):
    return _one(owner, "SELECT count(*) FROM customer_sessions WHERE token_sha256 = ANY(%s)", (list(tokens),))


def _snapshot(owner):
    """Число строк и контрольная сумма содержимого таблиц, которые очистка не должна менять."""
    return {table: _query(owner, f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
                                 f"FROM {table} t")[0] for table in KEPT_TABLES}


class _Observed:
    """Настоящее соединение psycopg пакета с наблюдением ВНУТРИ его транзакции (только чтение) и подменой ответа.

    after_purge(cursor, row) -> row вызывается сразу после SELECT auth_maintenance_purge(). keep_open=True —
    выход из `with` выполняет COMMIT/ROLLBACK без закрытия: сеанс проверяется после транзакции."""

    def __init__(self, conn, after_purge=None, keep_open=False):
        self.conn, self.after_purge, self.keep_open = conn, after_purge, keep_open

    @property
    def autocommit(self):
        return self.conn.autocommit

    def cursor(self):
        return _ObservedCursor(self.conn.cursor(), self.after_purge)

    def __enter__(self):
        self.conn.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb):
        if not self.keep_open:
            return self.conn.__exit__(exc_type, exc, tb)
        if exc_type:
            self.conn.rollback()
        else:
            self.conn.commit()
        return False

    def close(self):
        if not self.keep_open:
            self.conn.close()


class _ObservedCursor:
    def __init__(self, cursor, after_purge):
        self.cursor, self.after_purge, self.row = cursor, after_purge, None

    def __enter__(self):
        self.cursor.__enter__()
        return self

    def __exit__(self, *exc):
        return self.cursor.__exit__(*exc)

    def execute(self, sql, params=None):
        self.cursor.execute(sql, params)
        if sql == maintenance.SQL_PURGE:
            self.row = self.cursor.fetchone()
            if self.after_purge is not None:
                self.row = self.after_purge(self.cursor, self.row)

    def fetchone(self):
        return self.row


def _inside(cursor):
    cursor.execute(INSIDE)
    return cursor.fetchone()


# ---------- 1–3, 12. что удаляется и что остаётся ----------

def test_expired_counters_are_purged_and_live_ones_kept(lab):
    _counters(lab.owner, expired=5, live=3)
    assert maintenance.run_pass(lab.connect) is True
    assert _expired_counters(lab.owner) == 0 and _one(lab.owner, "SELECT count(*) FROM auth_throttle") == 3


def test_old_sessions_are_purged_and_recent_and_valid_ones_kept(lab):
    _, valid = _register(lab.owner)                                         # действующая сессия регистрации
    old, recent = _old_and_recent_sessions(lab.owner)
    assert maintenance.run_pass(lab.connect) is True
    assert _sessions_present(lab.owner, old) == 0
    assert _sessions_present(lab.owner, recent + [valid]) == 3
    assert _call(lab.owner, "SELECT auth_session_resolve(%s)", (valid,))["valid"] is True     # сценарий 12


def test_customers_orders_links_payments_and_audit_are_untouched(lab, product):
    _, session = _register(lab.owner)
    _register(lab.owner, email=OTHER)
    with lab.owner.cursor() as cur:
        order_id = _new_order(cur, product)
        assert _link(cur, session, order_id) == {"result": "linked"}
    lab.owner.commit()
    assert _call(lab.owner, "SELECT customer_disable_core((SELECT id FROM customers WHERE email_norm = %s), "
                            "'тест 2f-5b', 'pytest')", (OTHER,)) == {"result": "ok"}        # аудит и отзыв сессий
    _counters(lab.owner, expired=4)
    _old_and_recent_sessions(lab.owner)
    before = _snapshot(lab.owner)
    assert before["customer_orders"][0] == 1 and before["audit_log"][0] >= 2   # регистрации (и отключение)
    assert maintenance.run_pass(lab.connect) is True
    assert _snapshot(lab.owner) == before
    assert _one(lab.owner, "SELECT customer_id FROM customer_orders WHERE order_id = %s", (order_id,)) == \
        _one(lab.owner, "SELECT id FROM customers WHERE email_norm = %s", (EMAIL,))


# ---------- 4–7. пакеты, транзакции, локальные тайм-ауты ----------

def test_ten_thousand_boundary_takes_a_second_batch_in_its_own_transaction(lab, caplog):
    _counters(lab.owner, expired=LIMIT + 1)
    seen, before_batch = [], []

    def connect():
        before_batch.append(_expired_counters(lab.owner))                # наблюдатель видит только зафиксированное
        return _Observed(lab.connect(), after_purge=lambda cursor, row: (seen.append(_inside(cursor)), row)[1])

    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert maintenance.run_pass(connect) is True
    assert before_batch == [LIMIT + 1, 1]                                 # второй пакет — после COMMIT первого
    assert [row[:2] for row in seen] == [("1500ms", "500ms")] * 2         # локальные тайм-ауты в каждой транзакции
    assert seen[0][2] != seen[1][2]                                       # разные транзакции
    assert _expired_counters(lab.owner) == 0
    assert "пакетов 2, удалено счётчиков 10001, сессий 0" in caplog.text


def test_max_batches_caps_the_transactions_and_the_next_pass_finishes(lab, caplog):
    _counters(lab.owner, expired=LIMIT + 1)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert maintenance.run_pass(lab.connect, max_batches=1) is True
    assert _expired_counters(lab.owner) == 1 and "достигнут предел пакетов" in caplog.text
    opened = len(lab.opened)
    assert maintenance.run_pass(lab.connect, max_batches=1) is True
    assert _expired_counters(lab.owner) == 0 and len(lab.opened) == opened + 1


def test_timeouts_are_transaction_local(lab):
    _counters(lab.owner, expired=2)
    probe = lab.connect()
    session_values = _query(probe, "SELECT current_setting('statement_timeout'), current_setting('lock_timeout')")
    defaults = _query(probe, "SELECT name, reset_val FROM pg_settings WHERE name IN ('statement_timeout', "
                             "'lock_timeout') ORDER BY name")
    probe.close()

    real, inside = lab.connect(), []
    observed = _Observed(real, keep_open=True, after_purge=lambda cursor, row: (inside.append(_inside(cursor)), row)[1])
    assert maintenance.purge_batch(lambda: observed) == {"throttle_deleted": 2, "sessions_deleted": 0}
    assert inside[0][:2] == ("1500ms", "500ms")
    # После COMMIT в том же сеансе — прежние значения сеанса; в новом сеансе — прежние значения по умолчанию.
    assert _query(real, "SELECT current_setting('statement_timeout'), current_setting('lock_timeout')") == \
        session_values == [("30s", "5s")]
    real.close()
    fresh = lab.connect()
    assert _query(fresh, "SELECT name, reset_val FROM pg_settings WHERE name IN ('statement_timeout', 'lock_timeout') "
                         "ORDER BY name") == defaults
    fresh.close()


# ---------- 8–9. ROLLBACK и ограниченный по времени отказ ----------

def _bad_answer(cursor, row):
    return ({**row[0], "unexpected": 1},)


def _sql_error(cursor, row):
    cursor.execute("SELECT 1 / 0")
    return row


@pytest.mark.parametrize("after_purge, logged", [(_bad_answer, "MaintenanceError"),
                                                 (_sql_error, "DivisionByZero, SQLSTATE 22012")],
                         ids=["ответ вне контракта", "ошибка SQL"])
def test_unexpected_answer_or_sql_error_rolls_the_batch_back(lab, caplog, after_purge, logged):
    _register(lab.owner)                                                    # владелец старых сессий
    _counters(lab.owner, expired=3)
    old, _ = _old_and_recent_sessions(lab.owner)
    assert _expired_counters(lab.owner) == 3 and _sessions_present(lab.owner, old) == 3     # данные есть до очистки
    deleted_inside = []

    def observe(cursor, row):
        cursor.execute("SELECT (SELECT count(*) FROM auth_throttle), "
                       "(SELECT count(*) FROM customer_sessions WHERE token_sha256 = ANY(%s))", (list(old),))
        deleted_inside.append(cursor.fetchone())                          # внутри транзакции строки уже удалены
        return after_purge(cursor, row)

    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert maintenance.run_pass(lambda: _Observed(lab.connect(), after_purge=observe)) is False
    assert deleted_inside == [(0, 0)]
    assert _expired_counters(lab.owner) == 3 and _sessions_present(lab.owner, old) == 3     # всё откатилось
    assert logged in caplog.text


@pytest.mark.parametrize("timeouts, logged", [({}, "LockNotAvailable, SQLSTATE 55P03"),
                                              ({"statement_timeout_ms": 300, "lock_timeout_ms": 10_000},
                                               "QueryCanceled, SQLSTATE 57014")],
                         ids=["lock_timeout 500 мс", "statement_timeout"])
def test_conflicting_table_lock_fails_fast_without_partial_deletion(lab, caplog, timeouts, logged):
    """Счётчики функция удаляет первыми, затем ждёт customer_sessions — откатываются и уже удалённые счётчики."""
    _register(lab.owner)
    _counters(lab.owner, expired=4)
    old, _ = _old_and_recent_sessions(lab.owner)
    assert _expired_counters(lab.owner) == 4 and _sessions_present(lab.owner, old) == 3     # данные есть до очистки
    holder, _ = lab.peer()
    _exec(holder, "LOCK TABLE customer_sessions IN EXCLUSIVE MODE")       # таблица временной схемы, своя транзакция
    try:
        started = time.monotonic()
        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert maintenance.run_pass(lab.connect, **timeouts) is False
        assert time.monotonic() - started < 5
    finally:
        holder.rollback()
    assert _expired_counters(lab.owner) == 4 and _sessions_present(lab.owner, old) == 3     # частичного удаления нет
    assert logged in caplog.text
    assert maintenance.run_pass(lab.connect) is True and _expired_counters(lab.owner) == 0  # после снятия блокировки


# ---------- 10–11. занятые строки и параллельные проходы ----------

def test_skip_locked_leaves_busy_rows_and_removes_them_after_release(lab):
    _register(lab.owner)
    tag = _counters(lab.owner, expired=3)
    old, _ = _old_and_recent_sessions(lab.owner)
    assert _expired_counters(lab.owner) == 3 and _sessions_present(lab.owner, old) == 3     # данные есть до очистки
    holder, _ = lab.peer()
    _exec(holder, "SELECT 1 FROM auth_throttle WHERE key_sha256 = sha256(convert_to(%s, 'UTF8')) FOR UPDATE",
          (f"{tag}:expired:1",))
    _exec(holder, "SELECT 1 FROM customer_sessions WHERE token_sha256 = %s FOR UPDATE", (old[0],))
    try:
        started = time.monotonic()
        assert maintenance.run_pass(lab.connect) is True                    # не ждёт занятые строки
        assert time.monotonic() - started < 3
        assert _expired_counters(lab.owner) == 1 and _sessions_present(lab.owner, old) == 1
    finally:
        holder.rollback()
    assert maintenance.run_pass(lab.connect) is True
    assert _expired_counters(lab.owner) == 0 and _sessions_present(lab.owner, old) == 0


def test_parallel_passes_neither_conflict_nor_hang(lab):
    _, valid = _register(lab.owner)
    _counters(lab.owner, expired=LIMIT + 1, live=2)
    old, recent = _old_and_recent_sessions(lab.owner)
    before = _snapshot(lab.owner)
    start = threading.Barrier(2, timeout=10)

    def one_pass():
        start.wait()
        return maintenance.run_pass(lab.connect)

    passes = [_Background(one_pass), _Background(one_pass)]
    for step in passes:
        step.join(30)
    assert [(step.error, step.result) for step in passes] == [(None, True), (None, True)]
    assert _expired_counters(lab.owner) == 0 and _one(lab.owner, "SELECT count(*) FROM auth_throttle") == 2
    assert _sessions_present(lab.owner, old) == 0 and _sessions_present(lab.owner, recent + [valid]) == 3
    assert _snapshot(lab.owner) == before


# ---------- точка входа ----------

def test_once_entry_point_on_the_real_database(lab, caplog):
    _counters(lab.owner, expired=2)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert maintenance.main(["--once"], env={}, connect=lab.connect) == 0
    assert _expired_counters(lab.owner) == 0
    assert "пакетов 1, удалено счётчиков 2, сессий 0" in caplog.text
    assert lab.schema not in caplog.text and os.environ["TEST_DB_NAME"] not in caplog.text
