"""PR6 Stage 2f-7: инварианты 0005, которые иначе не проверялись детерминированно (мутации M9, M9b, M10, M13, M8).

  * M9  — эпоха учётных данных в customer_session_customer: сессия с устаревшей эпохой недействительна, даже если
          она не отозвана (прежде эпоха всегда менялась вместе с отзывом — вторая защита не проверялась);
  * M9b — та же проверка эпохи в шаге (c) customer_order_link_new;
  * M10 — фиксированное окно счётчика попыток входа: попытки внутри окна его не сдвигают;
  * M13, M8 — сила блокировки строки покупателя во входе (FOR NO KEY UPDATE) и порядок блокировок
          счётчик (1) → покупатель (2) (K6). Без гонок: другое соединение берёт блокировку с NOWAIT
          (SQLSTATE 55P03 вместо ожидания), ожидание входа подтверждается по pg_stat_activity, без sleep.

Безопасность запуска (решение владельцев, как в test_account_maintenance_db.py):
  * только явно заданное окружение: PYTHON_DOTENV_DISABLED=1, TEST_DB_NAME, DB_HOST/DB_PORT/DB_USER и пароль из
    скрытого ввода; без них модуль пропускается ДО импорта database.py;
  * защита tests/conftest.py (только TEST_DB_NAME) и _lab_role_guard (test_account_http_db.py): в
    digital_store_pr6_stage1_test — только роль pr6_stage1_owner без прав суперпользователя;
  * всё — во временной схеме ds_mig_<10 hex> на тест (make_schema/at_0005/accounts из test_migration_0005.py);
    схема удаляется в teardown make_schema. Каждое соединение — фабрика _OrderLab (test_account_order_db.py):
    search_path только из временной схемы, до первого SQL проверяются current_database() и
    current_schemas(false) == [схема]; lock_timeout 5 с, statement_timeout 30 с,
    idle_in_transaction_session_timeout 60 с — параметры только этих сеансов; у функций 0005 свой lock_timeout 2 с;
    ожидание блокировки — не дольше 5 с (_wait_blocked), потоки — join(15);
  * незавершённые транзакции откатываются, все соединения закрываются до удаления схемы (lab.close_all);
  * ролей, расширений, баз, advisory-блокировок и глобальных настроек тесты не создают и не меняют; чужие процессы
    не завершаются; FOR … NOWAIT и FOR NO KEY UPDATE — только на строках таблиц временной схемы;
  * состояние «эпоха сменилась, сессия не отозвана» функции 0005 не создают (customer_sessions_invalidate
    отзывает сессии вместе со сменой эпохи) — его создаёт владелец временной схемы прямым UPDATE customers.auth_epoch
    одной строки (триггеров и ограничений, кроме auth_epoch >= 1, на этой колонке нет).
"""

import os

import pytest

# Значения, при которых python-dotenv 1.2 не читает .env (dotenv.main._load_dotenv_disabled).
_DOTENV_DISABLED_VALUES = {"1", "true", "t", "yes", "y"}

# Проверки — до импорта модулей, которые импортируют database.py (он вызывает load_dotenv() при импорте).
if os.environ.get("PYTHON_DOTENV_DISABLED", "").casefold() not in _DOTENV_DISABLED_VALUES:
    pytest.skip("инварианты аккаунтов на PostgreSQL: запуск только с PYTHON_DOTENV_DISABLED=1 (без чтения .env)",
                allow_module_level=True)
if "TEST_DB_NAME" not in os.environ:
    pytest.skip("инварианты аккаунтов на PostgreSQL: задайте TEST_DB_NAME явно", allow_module_level=True)

import hashlib  # noqa: E402

import psycopg  # noqa: E402

import database  # noqa: E402
from test_account_http_db import SCHEMA_RE, _lab_role_guard  # noqa: E402,F401  (_lab_role_guard — фикстура)
from test_account_order_db import SESSION_LIMITS, _OrderLab  # noqa: E402
from test_migration_0005 import (  # noqa: E402,F401  (фикстуры)
    CHANGE, EMAIL, LOGIN, _Background, _call, _customer_id, _exec, _link, _login, _new_order, _proof, _register,
    _token, _valid, _wait_blocked, accounts, at_0005, make_schema, product,
)
from test_migrations import _query, _run  # noqa: E402

OTHER = "other@example.test"
PROFILE = "SELECT customer_profile_view(%s)"
# Ключ счётчика входа — как в auth_login: sha256(convert_to('email:' || email_norm, 'UTF8')).
LOGIN_KEY = hashlib.sha256(f"email:{EMAIL}".encode("utf-8")).digest()
SHARE = "SELECT 1 FROM customers WHERE id = %s FOR SHARE NOWAIT"
KEY_SHARE = "SELECT 1 FROM customers WHERE id = %s FOR KEY SHARE NOWAIT"
LOCK_NOT_AVAILABLE = "55P03"


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
        lab.close_all()                                          # откат и закрытие до удаления схемы (make_schema)


def _write(conn, sql, params, *, rows):
    """Подготовка данных владельцем схемы: ровно rows строк, иначе ошибка подготовки (UPDATE без подходящей строки
    молча изменяет 0 строк — проверка прошла бы на неподготовленных данных)."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        written = cur.rowcount
    if written != rows:
        conn.rollback()
        raise AssertionError(f"подготовка данных: изменено {written} строк вместо {rows}")
    conn.commit()


def _shift_epoch(owner, customer_id, delta):
    """Только эпоха покупателя; его сессии не трогаются (их auth_epoch неизменен — триггер customer_sessions_guard)."""
    _write(owner, "UPDATE customers SET auth_epoch = auth_epoch + %s WHERE id = %s", (delta, customer_id), rows=1)


def _session_state(owner, session):
    """Каждое условие действительности сессии по отдельности (владелец схемы читает таблицы напрямую):
    (не отозвана, абсолютный срок не истёк, срок простоя не истёк, статус покупателя, флаг аккаунтов,
    эпоха сессии, эпоха покупателя). Запись сессии обязана существовать."""
    rows = _query(owner, "SELECT s.revoked_at IS NULL, s.expires_at > clock_timestamp(), "
                         "s.idle_expires_at > clock_timestamp(), c.status, a.accounts_enabled, s.auth_epoch, c.auth_epoch "
                         "FROM customer_sessions s JOIN customers c ON c.id = s.customer_id "
                         "CROSS JOIN auth_settings a WHERE a.singleton AND s.token_sha256 = %s", (session,))
    assert len(rows) == 1, "запись сессии должна существовать"
    return rows[0]


def _only_epoch_differs(owner, session):
    """Сессия не отозвана и не истекла, покупатель активен, аккаунты включены; отличается только эпоха."""
    state = _session_state(owner, session)
    assert state[:5] == (True, True, True, "active", True), state
    assert state[5] + 1 == state[6], state
    return state


def _nowait(conn, sql, params):
    """Блокировка строки без ожидания в собственной транзакции conn. -> None (строка взята) или SQLSTATE отказа.
    Транзакция пробы всегда откатывается: проба ничего не удерживает и никого не ждёт."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    except psycopg.errors.LockNotAvailable as exc:
        return exc.sqlstate
    finally:
        conn.rollback()
    assert rows == [(1,)], "проба должна найти ровно одну строку (иначе «свободна» ничего не доказывает)"
    return None


def _still_waiting(observer, pid):
    return _query(observer, "SELECT cardinality(pg_blocking_pids(%s)) > 0", (pid,))[0][0]


# ---------- M9: эпоха в customer_session_customer ----------

def test_m9_stale_epoch_alone_invalidates_an_unrevoked_session(lab):
    """M9 (в customer_session_customer нет s.auth_epoch = c.auth_epoch): сессия снова действительна —
    resolve даёт valid = true, профиль раскрывается, «выйти везде» отвечает ok. Тест падает на первой проверке."""
    owner = lab.owner
    _run(owner, "UPDATE auth_settings SET session_touch_interval = INTERVAL '0'")       # touch без ожидания интервала
    proof, session = _register(owner)
    _, control = _register(owner, email=OTHER)
    cid = _customer_id(owner)
    assert _valid(owner, session) is True                                             # исходно действительна
    assert _call(owner, PROFILE, (session,))["email"] == EMAIL
    hash_before = _query(owner, "SELECT password_hash FROM customers WHERE id = %s", (cid,))

    _shift_epoch(owner, cid, +1)                                                      # без отзыва сессий
    state = _only_epoch_differs(owner, session)

    assert _valid(owner, session) is False
    assert _call(owner, PROFILE, (session,)) is None
    assert _call(owner, "SELECT customer_orders_view(%s)", (session,)) is None
    assert _call(owner, "SELECT auth_session_touch(%s)", (session,)) == {"touched": False}
    assert _call(owner, "SELECT auth_sessions_revoke_all(%s)", (session,)) == {"result": "session_invalid"}
    assert _call(owner, CHANGE, (session, proof, 1, None, None, _proof(), 1, _token())) == \
        {"result": "session_invalid"}                                                 # верный пароль не помогает
    # Отказы ничего не изменили: ни эпоху, ни отзыв, ни хэш; аудита «выйти везде» и смены пароля нет.
    assert _session_state(owner, session) == state
    assert _query(owner, "SELECT password_hash FROM customers WHERE id = %s", (cid,)) == hash_before
    assert _query(owner, "SELECT count(*) FROM audit_log WHERE action IN "
                         "('auth.sessions_revoke_all', 'auth.password_change')") == [(0,)]
    # Контроль: сессия другого покупателя в той же схеме действительна — функции и флаг работают.
    assert _valid(owner, control) is True

    # Причина — только эпоха: эпоха возвращена → та же сессия снова действительна.
    _shift_epoch(owner, cid, -1)
    assert _valid(owner, session) is True
    assert _call(owner, PROFILE, (session,))["email"] == EMAIL
    assert _call(owner, "SELECT auth_session_touch(%s)", (session,)) == {"touched": True}


# ---------- M9b: эпоха в шаге (c) привязки заказа ----------

def test_m9b_order_link_rechecks_the_epoch_and_links_nothing(lab, product):
    """M9b (в шаге (c) customer_order_link_new нет s.auth_epoch = c.auth_epoch): привязка отвечает 'linked'
    вместо 'session_invalid'. M9 (только customer_session_customer) этот тест ловит иначе: customer_orders_view
    по сессии со старой эпохой отвечает [] вместо NULL."""
    owner = lab.owner
    _, session = _register(owner)
    _, other_session = _register(owner, email=OTHER)
    cid = _customer_id(owner)
    with owner.cursor() as cur:                                   # чужой заказ: привязан к OTHER и зафиксирован
        foreign = _new_order(cur, product)
        assert _link(cur, other_session, foreign) == {"result": "linked"}
    owner.commit()
    links = "SELECT order_id, customer_id FROM customer_orders ORDER BY order_id"
    links_before = _query(owner, links)
    orders_before = _query(owner, "SELECT count(*) FROM orders")
    assert links_before == [(foreign, _customer_id(owner, OTHER))]

    buyer, _ = lab.peer()
    with buyer.cursor() as cur:                                   # транзакция нового заказа (как create_order_with_access)
        order_id = _new_order(cur, product)
        cur.execute("SELECT count(*) FROM order_access WHERE order_id = %s", (order_id,))
        assert cur.fetchone() == (1,)                             # гостевой секрет уже выдан
        _shift_epoch(owner, cid, +1)                              # другое соединение, зафиксировано; сессия не отозвана
        _only_epoch_differs(owner, session)
        assert _link(cur, session, order_id) == {"result": "session_invalid"}
        cur.execute("SELECT count(*) FROM customer_orders WHERE order_id = %s OR customer_id = %s", (order_id, cid))
        assert cur.fetchone() == (0,)                             # и внутри транзакции заказа привязки нет
    buyer.rollback()                                              # как AccountOrders: отказ → откат всего заказа (401)
    assert _query(owner, links) == links_before
    assert _query(owner, "SELECT count(*) FROM orders") == orders_before

    # Доступа к заказам по сессии со старой эпохой нет — ни к чужому, ни к списку.
    assert _call(owner, "SELECT customer_order_view(%s, %s)", (session, foreign)) is None
    assert _call(owner, "SELECT customer_orders_view(%s)", (session,)) is None
    # Контроль: владелец чужого заказа его видит.
    assert _call(owner, "SELECT customer_order_view(%s, %s)", (other_session, foreign))["order"]["id"] == foreign

    # Причина — только эпоха: эпоха возвращена → та же сессия привязывает новый заказ.
    _shift_epoch(owner, cid, -1)
    with buyer.cursor() as cur:
        assert _link(cur, session, _new_order(cur, product)) == {"result": "linked"}
    buyer.rollback()


# ---------- M10: фиксированное окно счётчика входа ----------

def _attempt(conn, proof, window_start=None):
    """Одна попытка входа в своей транзакции и в ТОЙ ЖЕ транзакции (тот же now(), что видела функция) — строка
    счётчика и retry_after по формуле auth_login от начала окна window_start (по умолчанию — от записанного).
    -> (ответ, window_start, attempts, now(), retry_after по формуле)."""
    with conn.cursor() as cur:
        cur.execute(LOGIN, (EMAIL, proof, 1, None, None, _token(), None))
        result = cur.fetchone()[0]
        cur.execute("SELECT t.window_start, t.attempts, now(), greatest(1, ceil(extract(epoch FROM "
                    "coalesce(%s::timestamptz, t.window_start) + a.login_window - now())))::integer "
                    "FROM auth_throttle t CROSS JOIN auth_settings a "
                    "WHERE a.singleton AND t.scope = 1 AND t.key_sha256 = %s", (window_start, LOGIN_KEY))
        rows = cur.fetchall()
    conn.commit()
    assert len(rows) == 1, "строка счётчика попыток должна быть ровно одна"
    return (result,) + rows[0]


def test_m10_throttle_window_stays_where_the_first_attempt_opened_it(lab):
    """M10 (window_start = now() при каждой попытке): после второй попытки начало окна — её now(), а не исходное;
    retry_after в блокировке — полное окно (900 с) от этой попытки, а не остаток исходного окна."""
    owner = lab.owner
    proof, _ = _register(owner)
    _run(owner, "UPDATE auth_settings SET login_limit = 3")
    result, opened, attempts, now, _ = _attempt(owner, _proof())
    assert (result, attempts, opened) == ({"result": "invalid"}, 1, now)               # окно открыла первая попытка
    # Начало окна — на 5 минут раньше (окно 15 минут, следующие попытки остаются внутри него): сдвиг окна
    # попыткой заметен на 5 минут, а не на микросекунды между транзакциями.
    _write(owner, "UPDATE auth_throttle SET window_start = window_start - INTERVAL '5 minutes' "
                  "WHERE scope = 1 AND key_sha256 = %s", (LOGIN_KEY,), rows=1)
    start = _query(owner, "SELECT window_start FROM auth_throttle WHERE scope = 1 AND key_sha256 = %s",
                   (LOGIN_KEY,))[0][0]

    for expected_attempts in (2, 3):
        result, window_start, attempts, _, _ = _attempt(owner, _proof())
        assert (result, attempts, window_start) == ({"result": "invalid"}, expected_attempts, start)
    retries = []
    for expected_attempts in (4, 5, 6):                           # и верный пароль — блокировка без проверки
        result, window_start, attempts, _, from_start = _attempt(owner, proof, start)
        assert (result["result"], attempts, window_start) == ("throttled", expected_attempts, start)
        assert result["retry_after_seconds"] == from_start      # остаток ИСХОДНОГО окна при том же now()
        retries.append(from_start)
    assert retries == sorted(retries, reverse=True)               # следующая попытка блокировку не продлевает
    assert retries[0] < 900 - 4 * 60                              # не полное окно от попытки: ~600 с остатка


# ---------- M13, M8: сила и порядок блокировок во входе ----------

@pytest.mark.parametrize("correct", [True, False], ids=["верный пароль", "неверный пароль"])
def test_m13_m8_login_holds_no_key_update_on_the_customer_row(lab, correct):
    """Незавершённый вход держит строку покупателя FOR NO KEY UPDATE (блокировка — до bcrypt, при любом пароле):
    FOR SHARE NOWAIT другого соединения — 55P03, FOR KEY SHARE NOWAIT (проверки внешних ключей) — проходит.
    M13 (FOR SHARE) и M8 (без блокировки): FOR SHARE NOWAIT проходит — тест падает. Блокировка сильнее нужной
    (FOR UPDATE) не пропустила бы FOR KEY SHARE — тест тоже падает."""
    owner = lab.owner
    proof, _ = _register(owner)
    cid = _customer_id(owner)
    login, _ = lab.peer()
    probe, _ = lab.peer()
    assert _nowait(probe, SHARE, (cid,)) is None                  # до входа строка свободна

    session = _token()
    expected = {"result": "ok" if correct else "invalid"}
    assert _exec(login, LOGIN, (EMAIL, proof if correct else _proof(), 1, None, None, session, None)) == expected
    assert _nowait(probe, SHARE, (cid,)) == LOCK_NOT_AVAILABLE    # вход держит строку и не зафиксирован
    assert _nowait(probe, KEY_SHARE, (cid,)) is None

    login.commit()
    assert _nowait(probe, SHARE, (cid,)) is None                  # держала именно транзакция входа
    assert _valid(owner, session) is correct


def test_k6_login_locks_the_counter_before_waiting_for_the_customer(lab):
    """Порядок K6: счётчик (1) → покупатель (2). Строку покупателя держит другая транзакция; вход ждёт её
    (подтверждено по pg_stat_activity), уже удерживая строку счётчика: FOR UPDATE NOWAIT на ней — 55P03.
    Обратный порядок (покупатель → счётчик): вход ждёт, не тронув счётчик, — проба проходит, тест падает.
    M8: вход не ждёт строку покупателя — _wait_blocked падает через 5 с."""
    owner = lab.owner
    proof, _ = _register(owner)
    assert _login(owner, _proof())[0] == {"result": "invalid"}    # строка счётчика зафиксирована (attempts 1)
    cid = _customer_id(owner)
    holder, _ = lab.peer()
    login, login_pid = lab.peer()
    probe, _ = lab.peer()
    assert _exec(holder, "SELECT 1 FROM customers WHERE id = %s FOR NO KEY UPDATE", (cid,)) == 1
    session = _token()
    step = _Background(lambda: _exec(login, LOGIN, (EMAIL, proof, 1, None, None, session, None)))
    try:
        _wait_blocked(owner, login_pid)                           # вход ждёт строку покупателя
        counter = _nowait(probe, "SELECT 1 FROM auth_throttle WHERE scope = 1 AND key_sha256 = %s FOR UPDATE NOWAIT",
                          (LOGIN_KEY,))
        waiting = _still_waiting(owner, login_pid)
    finally:
        holder.rollback()                                         # вход получает строку (lock_timeout функции 2 с)
        step.join(15)
    # Вход ждал и во время пробы; иначе (истёк lock_timeout функции) проба ничего не доказывает.
    assert waiting, "вход перестал ждать до конца пробы — проверка порядка неубедительна"
    assert counter == LOCK_NOT_AVAILABLE
    assert step.error is None and step.result == {"result": "ok"}, (step.error, step.result)
    login.commit()
    assert _valid(owner, session) is True
