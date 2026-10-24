"""PR6: конкурентность аккаунтов на настоящем PostgreSQL (REV5 §2.3, ADDENDUM §1.3, ADDENDUM2 §1.3, §3.4).

K1–K5, K7, K8, K9 (новая), K10a–d, K11; эксперимент Г2 на настоящих функциях: A6, A8, B2–B4, B6 и A7 как
наблюдение. K6 и M13 — мутационные проверки, отдельный этап до merge.

Схема каждого теста — как в tests/test_digital_keys_concurrency.py: соединение держит транзакцию открытой,
другое в потоке выполняет конфликтующий шаг, ожидание подтверждается по pg_stat_activity / pg_blocking_pids,
затем первое фиксирует или откатывает. Всё — во временной схеме (make_schema) под владельцем схемы; функции
вызываются напрямую, административные — через ядра *_core (обёртки admin_* требуют ролей: K10a под ролями —
tests/test_customer_auth_privileges.py). bcrypt cost 4 — только в этой изолированной схеме. lock_timeout
функций аккаунтов — 2 с: удерживающая транзакция фиксируется сразу после подтверждённого ожидания.
"""

import hashlib
import os
import time

import psycopg
import pytest

from test_migration_0005 import (  # noqa: F401  (фикстуры)
    CHANGE, EMAIL, LOGIN, REGISTER, _Background, _call, _customer_id, _exec, _link, _login, _new_order, _proof,
    _register, _short_session, _token, _valid, _wait_blocked, accounts, at_0005, make_schema, peers, product,
)
from test_migrations import _query, _run


def _sessions(conn):
    return _query(conn, "SELECT count(*) FROM customer_sessions")[0][0]


def _in_thread(conn, sql, params=None, commit=True):
    """Шаг в отдельном потоке: вызов и (по умолчанию) COMMIT. -> _Background."""
    def run():
        value = _exec(conn, sql, params)
        if commit:
            conn.commit()
        return value
    return _Background(run)


def _order_with_link(conn, product_id, session, token=None):
    """Транзакция заказа без COMMIT: заказ, гостевой секрет, привязка. -> (id заказа, ответ привязки)."""
    with conn.cursor() as cur:
        order_id = _new_order(cur, product_id, token_sha256=token)
        return order_id, _link(cur, session, order_id)


def _joined(step, timeout=15):
    step.join(timeout)
    assert step.error is None, repr(step.error)
    return step.result


# ---------- K1–K5: вход и смена пароля (REV5 §2.3) ----------

def test_k1_login_waiting_for_committed_password_change_is_invalid(accounts, peers):
    conn, _ = accounts
    proof, session = _register(conn)
    changer, _ = peers()
    login, login_pid = peers()
    assert _exec(changer, CHANGE, (session, proof, 1, None, None, _proof(), 1, _token())) == {"result": "ok"}
    step = _in_thread(login, LOGIN, (EMAIL, proof, 1, None, None, _token(), None))
    _wait_blocked(conn, login_pid)
    changer.commit()
    assert _joined(step) == {"result": "invalid"}
    assert _sessions(conn) == 2                     # регистрация (отозвана) + новая сессия смены пароля


def test_k2_login_waiting_for_rolled_back_password_change_is_ok(accounts, peers):
    conn, _ = accounts
    proof, session = _register(conn)
    changer, _ = peers()
    login, login_pid = peers()
    assert _exec(changer, CHANGE, (session, proof, 1, None, None, _proof(), 1, _token())) == {"result": "ok"}
    step = _in_thread(login, LOGIN, (EMAIL, proof, 1, None, None, _token(), None))
    _wait_blocked(conn, login_pid)
    changer.rollback()
    assert _joined(step) == {"result": "ok"}


INVALIDATE_AFTER_LOGIN = {
    # K3 — смена пароля; K4 — «выйти везде» и отключение покупателя.
    "k3_password_change": lambda cid, session, proof: (CHANGE, (session, proof, 1, None, None, _proof(), 1, _token())),
    "k4_sessions_revoke_all": lambda cid, session, proof: ("SELECT auth_sessions_revoke_all(%s)", (session,)),
    "k4_customer_disable": lambda cid, session, proof: ("SELECT customer_disable_core(%s, 'K4', 'pytest')", (cid,)),
}


@pytest.mark.parametrize("kind", INVALIDATE_AFTER_LOGIN)
def test_k3_k4_session_of_earlier_login_is_invalid_after_invalidation(accounts, peers, kind):
    conn, _ = accounts
    proof, reg_session = _register(conn)
    login, _ = peers()
    invalidator, invalidator_pid = peers()
    login_session = _token()
    assert _exec(login, LOGIN, (EMAIL, proof, 1, None, None, login_session, None)) == {"result": "ok"}
    sql, params = INVALIDATE_AFTER_LOGIN[kind](_customer_id(conn), reg_session, proof)
    step = _in_thread(invalidator, sql, params)
    _wait_blocked(conn, invalidator_pid)
    login.commit()
    assert _joined(step)["result"] == "ok"
    assert _valid(conn, login_session) is False
    assert _query(conn, "SELECT s.revoked_at IS NOT NULL, s.auth_epoch < c.auth_epoch FROM customer_sessions s "
                        "JOIN customers c ON c.id = s.customer_id WHERE s.token_sha256 = %s",
                  (login_session,)) == [(True, True)]


def test_k5_parallel_wrong_logins_and_password_change_no_deadlock(accounts, peers):
    conn, _ = accounts
    proof, session = _register(conn)
    steps = []
    for _ in range(20):
        login, _ = peers()
        steps.append(_in_thread(login, LOGIN, (EMAIL, _proof(), 1, None, None, _token(), None)))
    changer, _ = peers()
    change = _in_thread(changer, CHANGE, (session, proof, 1, None, None, _proof(), 1, _token()))
    results = [_joined(step, 30)["result"] for step in steps]          # ошибка 40P01 упала бы здесь
    assert _joined(change, 30) == {"result": "ok"}
    assert set(results) <= {"invalid", "throttled"}, results
    assert _query(conn, "SELECT attempts FROM auth_throttle WHERE scope = 1") == [(20,)]


# ---------- K7, K8: пересчёт хэша под новую версию перца (ADDENDUM §1.3) ----------

def test_k7_password_change_waits_for_rehashing_login(accounts, peers):
    conn, _ = accounts
    v1, reg_session = _register(conn, ver=1)
    v2, new_v2 = _proof(), _proof()                  # v2 — тот же пароль под перцем версии 2
    login, _ = peers()
    changer, changer_pid = peers()
    login_session = _token()
    assert _exec(login, LOGIN, (EMAIL, v2, 2, v1, 1, login_session, None)) == {"result": "ok"}
    step = _in_thread(changer, CHANGE, (reg_session, v2, 2, None, None, new_v2, 2, _token()))
    _wait_blocked(conn, changer_pid)
    login.commit()
    assert _joined(step) == {"result": "ok"}         # текущий пароль проверен по ПЕРЕСЧИТАННОМУ хэшу
    assert _query(conn, "SELECT pepper_version, auth_epoch FROM customers") == [(2, 2)]
    assert _valid(conn, login_session) is False
    assert _login(conn, new_v2, ver=2)[0] == {"result": "ok"}
    assert _login(conn, v2, ver=2)[0] == {"result": "invalid"}


def test_k8_old_pepper_login_waiting_for_password_change_is_invalid_without_rehash(accounts, peers):
    conn, _ = accounts
    v1, reg_session = _register(conn, ver=1)
    v2_old, new_v2 = _proof(), _proof()
    changer, _ = peers()
    login, login_pid = peers()
    assert _exec(changer, CHANGE, (reg_session, v2_old, 2, v1, 1, new_v2, 2, _token())) == {"result": "ok"}
    step = _in_thread(login, LOGIN, (EMAIL, v2_old, 2, v1, 1, _token(), None))
    _wait_blocked(conn, login_pid)
    changer.commit()
    assert _joined(step) == {"result": "invalid"}
    assert _sessions(conn) == 2                       # по старому паролю сессий нет
    assert _login(conn, new_v2, ver=2)[0] == {"result": "ok"}     # хэш — нового пароля, не пересчитан заново
    assert _query(conn, "SELECT count(*) FROM audit_log WHERE action = 'auth.login' "
                        "AND (details->>'rehashed')::boolean") == [(0,)]


# ---------- K9, K10a–d: привязка заказа (ADDENDUM2 §3.4) ----------

def test_k9_link_waits_for_login_and_links(accounts, product, peers):
    conn, _ = accounts
    proof, session = _register(conn)
    login, _ = peers()
    buyer, buyer_pid = peers()
    assert _exec(login, LOGIN, (EMAIL, proof, 1, None, None, _token(), None)) == {"result": "ok"}
    step = _Background(lambda: _order_with_link(buyer, product, session))
    _wait_blocked(conn, buyer_pid)
    login.commit()
    order_id, result = _joined(step)
    assert result == {"result": "linked"}
    buyer.commit()
    assert _query(conn, "SELECT customer_id FROM customer_orders WHERE order_id = %s", (order_id,)) \
        == [(_customer_id(conn),)]


# K10a/K10b/K10c — освобождение email; K10d — те же сценарии для смены пароля, «выйти везде» и отключения.
INVALIDATORS = {
    "release_email": lambda cid, other, proof: ("SELECT customer_release_email_core(%s, 'K10', 'pytest')", (cid,)),
    "password_change": lambda cid, other, proof: (CHANGE, (other, proof, 1, None, None, _proof(), 1, _token())),
    "sessions_revoke_all": lambda cid, other, proof: ("SELECT auth_sessions_revoke_all(%s)", (other,)),
    "customer_disable": lambda cid, other, proof: ("SELECT customer_disable_core(%s, 'K10', 'pytest')", (cid,)),
}


def _k10_setup(conn, kind):
    """Покупатель с двумя сессиями: link_session — для привязки, other — для смены пароля / «выйти везде»."""
    proof, link_session = _register(conn)
    _, other = _login(conn, proof)
    return link_session, INVALIDATORS[kind](_customer_id(conn), other, proof)


@pytest.mark.parametrize("kind", INVALIDATORS)
def test_k10a_k10d_link_waits_for_invalidation_and_refuses(accounts, product, peers, kind):
    conn, _ = accounts
    link_session, (sql, params) = _k10_setup(conn, kind)
    invalidator, _ = peers()
    buyer, buyer_pid = peers()
    assert _exec(invalidator, sql, params)["result"] == "ok"
    orders_before = _query(conn, "SELECT count(*) FROM orders")
    stock_before = _query(conn, "SELECT quantity FROM products WHERE id = %s", (product,))
    step = _Background(lambda: _order_with_link(buyer, product, link_session))
    _wait_blocked(conn, buyer_pid)
    invalidator.commit()
    _, result = _joined(step)
    assert result == {"result": "session_invalid"}
    buyer.rollback()                                   # Python откатывает всю транзакцию заказа → 401
    assert _query(conn, "SELECT count(*) FROM orders") == orders_before
    assert _query(conn, "SELECT quantity FROM products WHERE id = %s", (product,)) == stock_before
    assert _query(conn, "SELECT count(*) FROM customer_orders") == [(0,)]


@pytest.mark.parametrize("kind", INVALIDATORS)
def test_k10b_k10d_invalidation_waits_for_link_and_order_stays_linked(accounts, product, peers, kind):
    conn, _ = accounts
    link_session, (sql, params) = _k10_setup(conn, kind)
    cid = _customer_id(conn)
    buyer, _ = peers()
    invalidator, invalidator_pid = peers()
    token = hashlib.sha256(os.urandom(32)).digest()
    order_id, linked = _order_with_link(buyer, product, link_session, token)
    assert linked == {"result": "linked"}
    step = _in_thread(invalidator, sql, params)
    _wait_blocked(conn, invalidator_pid)
    buyer.commit()
    assert _joined(step)["result"] == "ok"
    assert _query(conn, "SELECT customer_id FROM customer_orders WHERE order_id = %s", (order_id,)) == [(cid,)]
    assert _valid(conn, link_session) is False
    if kind == "release_email":
        assert _query(conn, "SELECT email_norm FROM customers WHERE id = %s", (cid,)) == [(f"released+{cid}@invalid",)]
    # Гостевой access_token заказа работает и после смены эпохи / освобождения email.
    assert _call(conn, "SELECT order_status_view(%s, %s)", (order_id, token))["order"]["id"] == order_id


@pytest.mark.parametrize("kind", INVALIDATORS)
def test_k10c_rolled_back_invalidation_lets_waiting_link_succeed(accounts, product, peers, kind):
    conn, _ = accounts
    link_session, (sql, params) = _k10_setup(conn, kind)
    invalidator, _ = peers()
    buyer, buyer_pid = peers()
    assert _exec(invalidator, sql, params)["result"] == "ok"
    step = _Background(lambda: _order_with_link(buyer, product, link_session))
    _wait_blocked(conn, buyer_pid)
    invalidator.rollback()
    order_id, result = _joined(step)
    assert result == {"result": "linked"}
    buyer.commit()
    assert _query(conn, "SELECT count(*) FROM customer_orders WHERE order_id = %s", (order_id,)) == [(1,)]


# ---------- K11: смешанная нагрузка (ADDENDUM §1.3) ----------

def test_k11_parallel_old_pepper_logins_password_change_and_links(accounts, product, peers):
    conn, _ = accounts
    _run(conn, "UPDATE auth_settings SET login_limit = 1000")
    v1, reg_session = _register(conn, ver=1)
    _, link_session = _login(conn, v1, ver=1)
    v2, new_v2 = _proof(), _proof()
    steps = []
    for _ in range(50):
        login, _ = peers()
        steps.append(("login", _in_thread(login, LOGIN, (EMAIL, v2, 2, v1, 1, _token(), None))))
    changer, _ = peers()
    steps.append(("change", _in_thread(changer, CHANGE, (reg_session, v2, 2, v1, 1, new_v2, 2, _token()))))
    for _ in range(5):
        buyer, _ = peers()

        def order_transaction(buyer=buyer):
            order_id, result = _order_with_link(buyer, product, link_session)
            (buyer.commit if result == {"result": "linked"} else buyer.rollback)()
            return result
        steps.append(("link", _Background(order_transaction)))

    results = {"login": [], "change": [], "link": []}
    for kind, step in steps:
        results[kind].append(_joined(step, 60)["result"])     # 40P01 (взаимоблокировка) упала бы здесь
    # 'busy' — законный ответ под нагрузкой (ожидание дольше lock_timeout), но не взаимоблокировка.
    assert set(results["login"]) <= {"ok", "invalid", "busy"}, results["login"]
    assert results["change"] == ["ok"]
    assert set(results["link"]) <= {"linked", "session_invalid"}, results["link"]
    assert _query(conn, "SELECT pepper_version FROM customers") == [(2,)]
    assert _login(conn, new_v2, ver=2)[0] == {"result": "ok"}     # хэш — нового пароля
    assert _login(conn, v2, ver=2)[0] == {"result": "invalid"}


# ---------- touch и отзыв сессий ----------

def test_touch_skips_session_locked_by_revoke_all_and_never_revives(accounts, peers):
    conn, _ = accounts
    _run(conn, "UPDATE auth_settings SET session_touch_interval = INTERVAL '0'")
    _, session = _register(conn)
    idle_before = _query(conn, "SELECT idle_expires_at FROM customer_sessions")
    revoker, _ = peers()
    toucher, _ = peers()
    assert _exec(revoker, "SELECT auth_sessions_revoke_all(%s)", (session,)) == {"result": "ok"}
    started = time.monotonic()
    assert _exec(toucher, "SELECT auth_session_touch(%s)", (session,)) == {"touched": False}
    assert time.monotonic() - started < 1.0          # SKIP LOCKED: не ждёт строку, которую держит отзыв
    toucher.commit()
    revoker.commit()
    assert _call(conn, "SELECT auth_session_touch(%s)", (session,)) == {"touched": False}
    assert _valid(conn, session) is False
    assert _query(conn, "SELECT idle_expires_at FROM customer_sessions") == idle_before


@pytest.mark.parametrize("revoke", ["SELECT auth_sessions_revoke_all(%s)", "SELECT auth_session_revoke(%s)"])
def test_revoke_waits_for_uncommitted_touch_and_wins(accounts, peers, revoke):
    conn, _ = accounts
    _run(conn, "UPDATE auth_settings SET session_touch_interval = INTERVAL '0'")
    _, session = _register(conn)
    toucher, _ = peers()
    revoker, revoker_pid = peers()
    assert _exec(toucher, "SELECT auth_session_touch(%s)", (session,)) == {"touched": True}
    step = _in_thread(revoker, revoke, (session,))
    _wait_blocked(conn, revoker_pid)
    toucher.commit()
    assert _joined(step) == {"result": "ok"}
    assert _valid(conn, session) is False
    assert _call(conn, "SELECT auth_session_touch(%s)", (session,)) == {"touched": False}


def test_revoke_all_refused_when_session_expires_while_waiting(accounts, peers):
    conn, _ = accounts
    _register(conn)
    short = _short_session(conn)
    holder, _ = peers()
    revoker, revoker_pid = peers()
    _exec(holder, "SELECT 1 FROM customers WHERE id = %s FOR NO KEY UPDATE", (_customer_id(conn),))
    step = _in_thread(revoker, "SELECT auth_sessions_revoke_all(%s)", (short,))
    _wait_blocked(conn, revoker_pid)
    time.sleep(1.3)                                   # меньше lock_timeout функции (2 с)
    holder.commit()
    assert _joined(step) == {"result": "session_invalid"}
    assert _query(conn, "SELECT auth_epoch FROM customers") == [(1,)]


# ---------- регистрация одновременно со сменой стоимости bcrypt ----------

def test_parallel_registrations_and_cost_change_keep_one_hash_cost(accounts, peers):
    """Пустая таблица покупателей, 5 регистраций и смена стоимости одновременно: какой бы ни была очерёдность,
    у всех сохранённых хэшей и у фиктивного — одна стоимость. Регистрация, которая посчитала хэш по старой
    стоимости, ждёт COMMIT смены стоимости и отклоняется триггером (или получает 'busy')."""
    conn, _ = accounts
    steps = []
    for n in range(5):
        registrar, _ = peers()
        steps.append(_in_thread(registrar, REGISTER, (f"buyer{n}@example.test", _proof(), 1, _token())))
    changer, _ = peers()
    steps.append(_in_thread(changer, "UPDATE auth_settings SET bcrypt_cost = 5"))
    for step in steps:
        step.join(30)
        if step.error is not None:
            assert isinstance(step.error, psycopg.errors.RaiseException), repr(step.error)
            assert "стоимость bcrypt" in str(step.error) or "до первого покупателя" in str(step.error)
        elif step.result is not None:
            assert step.result["result"] in {"created", "busy"}
    costs = _query(conn, "SELECT DISTINCT left(password_hash, 7) FROM customers")
    dummy = _query(conn, "SELECT left(dummy_hash, 7) FROM auth_settings")[0][0]
    assert all(prefix == dummy for (prefix,) in costs), (costs, dummy)


# ---------- Г2 на настоящих функциях (ADDENDUM §4.1, ADDENDUM2 §1.3) ----------

def test_g2_a5_a6_register_busy_even_without_session_lock_timeout(accounts, peers):
    """A6: сеанс с lock_timeout = 0 — SET функции побеждает, 'busy' через ~2 с. A5: SET не утекает в сеанс."""
    conn, _ = accounts
    holder, _ = peers()
    second, _ = peers()
    assert _exec(holder, REGISTER, ("x@example.test", _proof(), 1, _token())) == {"result": "created"}
    _exec(second, "SET lock_timeout = 0")
    second.commit()
    started = time.monotonic()
    assert _exec(second, REGISTER, ("x@example.test", _proof(), 1, _token())) == {"result": "busy"}
    assert 1.5 <= time.monotonic() - started < 5
    assert _exec(second, "SELECT 1") == 1             # A4: транзакция после 'busy' исправна
    second.commit()
    assert _exec(second, "SHOW lock_timeout") == "0"
    holder.rollback()


def test_g2_a8_parallel_registrations_of_one_email_create_exactly_one(accounts, peers):
    conn, _ = accounts
    holder, _ = peers()
    assert _exec(holder, REGISTER, ("x@example.test", _proof(), 1, _token())) == {"result": "created"}
    waiting = []
    for _ in range(10):
        registrar, pid = peers()
        waiting.append((pid, _in_thread(registrar, REGISTER, ("x@example.test", _proof(), 1, _token()))))
    for pid, _ in waiting:
        _wait_blocked(conn, pid)
    holder.rollback()
    results = [_joined(step, 30)["result"] for _, step in waiting]
    assert results.count("created") == 1, results
    assert set(results) <= {"created", "email_taken", "busy"}, results
    assert _query(conn, "SELECT count(*) FROM customers WHERE email_norm = 'x@example.test'") == [(1,)]


def test_g2_a7_statement_timeout_observation(accounts, peers, record_property):
    """A7 — наблюдение, без заранее заданного ожидаемого исхода: короткий statement_timeout сеанса при ожидании
    в регистрации. Фиксируется фактический исход; проверяется только безопасность: B ничего не создал,
    соединение после ROLLBACK исправно."""
    conn, _ = accounts
    holder, _ = peers()
    second, _ = peers()
    assert _exec(holder, REGISTER, ("x@example.test", _proof(), 1, _token())) == {"result": "created"}
    _exec(second, "SET statement_timeout = '300ms'")
    second.commit()
    started = time.monotonic()
    try:
        outcome = _exec(second, REGISTER, ("x@example.test", _proof(), 1, _token()))["result"]
    except psycopg.Error as exc:
        outcome = f"{type(exc).__name__} ({exc.sqlstate})"
    elapsed = round(time.monotonic() - started, 2)
    second.rollback()
    record_property("g2_a7_outcome", outcome)
    record_property("g2_a7_seconds", elapsed)
    print(f"Г2 A7: исход {outcome}, {elapsed} с")
    holder.rollback()
    _exec(second, "RESET statement_timeout")
    assert _exec(second, "SELECT 1") == 1
    second.commit()
    assert _query(conn, "SELECT count(*) FROM customers WHERE email_norm = 'x@example.test'") == [(0,)]


def test_g2_b2_b3_busy_on_customer_lock_keeps_counter_and_releases_it(accounts, peers):
    """B2: вход ждёт строку покупателя → 'busy', счётчик не изменился. B3: блокировка строки счётчика из
    откатанной подтранзакции снята — третье соединение не ждёт."""
    conn, _ = accounts
    proof, _ = _register(conn)
    _login(conn, _proof())                            # строка счётчика есть, attempts = 1
    holder, _ = peers()
    login, _ = peers()
    third, _ = peers()
    _exec(holder, "SELECT 1 FROM customers WHERE email_norm = %s FOR NO KEY UPDATE", (EMAIL,))
    assert _exec(login, LOGIN, (EMAIL, proof, 1, None, None, _token(), None)) == {"result": "busy"}
    _exec(third, "SET lock_timeout = '100ms'")
    _exec(third, "UPDATE auth_throttle SET attempts = attempts WHERE scope = 1")   # без ожидания
    third.commit()
    login.commit()
    holder.commit()
    assert _query(conn, "SELECT attempts FROM auth_throttle WHERE scope = 1") == [(1,)]


def test_g2_b4_busy_does_not_depend_on_password(accounts, peers):
    """B4: при занятом счётчике ответ для верного и неверного пароля одинаков и без bcrypt."""
    conn, _ = accounts
    proof, _ = _register(conn)
    holder, _ = peers()
    right, _ = peers()
    wrong, _ = peers()
    assert _exec(holder, LOGIN, (EMAIL, _proof(), 1, None, None, _token(), None)) == {"result": "invalid"}
    outcomes = []
    for conn_b, attempt in ((right, proof), (wrong, _proof())):
        outcomes.append(_exec(conn_b, LOGIN, (EMAIL, attempt, 1, None, None, _token(), None)))
        conn_b.commit()
    holder.commit()
    assert outcomes == [{"result": "busy"}, {"result": "busy"}]
    assert _query(conn, "SELECT attempts FROM auth_throttle WHERE scope = 1") == [(1,)]


def test_g2_b6_login_busy_even_without_session_lock_timeout_and_no_leak(accounts, peers):
    """B6 = A5 и A6 для входа."""
    conn, _ = accounts
    proof, _ = _register(conn)
    holder, _ = peers()
    second, _ = peers()
    _exec(holder, LOGIN, (EMAIL, _proof(), 1, None, None, _token(), None))
    _exec(second, "SET lock_timeout = 0")
    second.commit()
    started = time.monotonic()
    assert _exec(second, LOGIN, (EMAIL, proof, 1, None, None, _token(), None)) == {"result": "busy"}
    assert 1.5 <= time.monotonic() - started < 5
    second.commit()
    assert _exec(second, "SHOW lock_timeout") == "0"
    holder.rollback()
