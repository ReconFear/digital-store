"""PR6 этап 2c на настоящем PostgreSQL: AccountService (accounts/service.py) против функций миграции 0005.

Офлайн-тесты (test_account_service.py) проверяют сервис на поддельных соединениях. Здесь — то, что без
сервера не проверить: передача и разбор JSONB, bytea и ::smallint; настоящие исключения psycopg и SQLSTATE
(RAISE, statement_timeout, нарушение уникальности, обрыв соединения при COMMIT); один resolve после
настоящего сбоя COMMIT; фиксация отказов; семафор bcrypt, пока вызов ждёт блокировку в базе.

Где и под кем:
  * только база TEST_DB_NAME (её задают явно; без неё модуль пропускается) под защитой tests/conftest.py;
  * каждый тест — во временной схеме ds_mig_* (фикстуры make_schema/at_0005/accounts из
    test_migration_0005.py): миграции 0000–0005, bcrypt cost 4 и включённые флаги — только в этой схеме;
    схема удаляется после теста. Таблицы public тестовой базы не затрагиваются;
  * роль — та же, что у остальных тестов с базой (DB_USER); ролей и прав тесты не создают и не меняют.
    Нужны: CREATE на тестовой базе и USAGE на ext_pgcrypto (как для Stage 1). Тест сбоя COMMIT завершает
    свой же серверный процесс (pg_terminate_backend своей роли).

Привязка заказа (customer_order_link_new) в сервис не входит — заказ привязывается здесь SQL-помощником
Stage 1 только для проверки просмотров. Права ролей app/worker/admin — test_customer_auth_privileges.py.
"""

import os
import time
import unicodedata

import psycopg
import pytest

from accounts.config import AccountSettings, Pepper
from accounts.proof import password_proof
from accounts.service import (
    SQL_RESOLVE, AccountInputError, AccountService, AccountServiceError, SessionOutcome, check_email_norm,
)
from accounts.session import SessionToken
from test_migration_0005 import (  # noqa: F401  (фикстуры)
    EMAIL, _Background, _exec, _link, _new_order, _wait_blocked, accounts, at_0005, make_schema, peers, product,
)
from test_migrations import _query, _run

# Только явно выбранная база: без TEST_DB_NAME модуль пропускается (conftest всё равно отказал бы в подключении).
pytestmark = pytest.mark.skipif("TEST_DB_NAME" not in os.environ,
                                reason="интеграция сервиса аккаунтов: задайте TEST_DB_NAME явно")

# Тестовые значения (не секреты): перцы и ключ CSRF только для этих тестов.
PEPPER_1 = Pepper(1, bytes(range(32)))
PEPPER_2 = Pepper(2, bytes(range(32, 64)))
PEPPER_3 = Pepper(3, bytes(range(96, 128)))
CSRF_KEY = bytes(range(64, 96))
SETTINGS = AccountSettings(enabled=True, pepper_current=PEPPER_1, csrf_key=CSRF_KEY)
ROTATING = AccountSettings(enabled=True, pepper_current=PEPPER_2, pepper_previous=PEPPER_1, csrf_key=CSRF_KEY)
ROTATED = AccountSettings(enabled=True, pepper_current=PEPPER_2, csrf_key=CSRF_KEY)
UNKNOWN_VERSION = AccountSettings(enabled=True, pepper_current=PEPPER_3, csrf_key=CSRF_KEY)

OTHER = "other@example.test"
PASSWORD = "Пароль ёлки 2026"          # «ё» в NFD — две кодовые точки: проверка NFC до HMAC
NEW_PASSWORD = "Новый пароль ёлки 2027"
WRONG_PASSWORD = "Неверный пароль 2026"


# ---------- лаборатория: сервис на соединениях временной схемы ----------

class _CommitHook:
    """Настоящее соединение, у которого COMMIT выполняет hook(conn) — сбой фиксации на настоящем сервере."""

    def __init__(self, conn, hook):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_hook", hook)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def commit(self):
        self._hook(self._conn)


class _Lab:
    """Фабрика соединений для AccountService: database.get_connection (защита conftest) + search_path
    временной схемы. opened — [(соединение, pid)] в порядке открытия."""

    def __init__(self, owner, schema):
        self.owner = owner                 # соединение владельца схемы: наблюдение и настройки auth_settings
        self.schema = schema
        self.opened = []
        self.session_setup = []            # SET для каждого нового соединения
        self.commit_hooks = []             # перехват COMMIT для следующих соединений (None — без перехвата)

    def connect(self):
        import database
        conn = database.get_connection()
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {self.schema}")         # имя схемы сгенерировано make_schema
            for sql in self.session_setup:
                cur.execute(sql)
            cur.execute("SELECT pg_backend_pid()")
            pid = cur.fetchone()[0]
        conn.commit()                      # иначе сервис не сможет выставить уровень изоляции
        self.opened.append((conn, pid))
        hook = self.commit_hooks.pop(0) if self.commit_hooks else None
        return _CommitHook(conn, hook) if hook is not None else conn

    def service(self, settings=SETTINGS, slots=2):
        return AccountService(settings, connect=self.connect, bcrypt_slots=slots)


@pytest.fixture
def lab(accounts):
    owner, schema = accounts
    lab = _Lab(owner, schema)
    yield lab
    for conn, _ in lab.opened:             # до удаления схемы (make_schema): открытых транзакций не остаётся
        if not conn.closed:
            conn.close()


def _service_error(call, code):
    with pytest.raises(AccountServiceError) as error:
        call()
    assert error.value.code == code
    return error.value


def _state(conn):
    """Зафиксированные данные аккаунтов (другое соединение видит только их)."""
    return (_query(conn, "SELECT id, email_norm, password_hash, pepper_version, auth_epoch FROM customers ORDER BY id"),
            _query(conn, "SELECT token_sha256, revoked_at FROM customer_sessions ORDER BY id"),
            _query(conn, "SELECT scope, key_sha256, attempts FROM auth_throttle ORDER BY scope, key_sha256"),
            _query(conn, "SELECT count(*) FROM audit_log"))


def _tables_containing(conn, needle):
    found = []
    for table in ("customers", "customer_sessions", "auth_throttle", "audit_log"):
        if _query(conn, f"SELECT count(*) FROM {table} t WHERE strpos(row_to_json(t)::text, %s) > 0", (needle,))[0][0]:
            found.append(table)
    return found


def _audit(conn, action):
    return _query(conn, "SELECT count(*) FROM audit_log WHERE action = %s", (action,))[0][0]


def _wait_new_connection(lab, count, timeout=5.0):
    """pid соединения, открытого сервисом после count уже открытых."""
    deadline = time.monotonic() + timeout
    while len(lab.opened) <= count:
        assert time.monotonic() < deadline, "сервис не открыл соединение"
        time.sleep(0.01)
    return lab.opened[count][1]


def _lock_customer(peers, email=EMAIL):
    holder, _ = peers()
    _exec(holder, "SELECT 1 FROM customers WHERE email_norm = %s FOR NO KEY UPDATE", (email,))
    return holder


# ---------- контракт: типы, результаты, данные в базе ----------

def test_register_and_login_pass_real_types_and_store_no_secrets(lab):
    service = lab.service()
    assert unicodedata.normalize("NFD", PASSWORD) != PASSWORD
    created = service.register(EMAIL, unicodedata.normalize("NFD", PASSWORD))
    assert created.result == "created" and created.session is not None
    assert service.resolve(created.session) is True
    # bytea туда и обратно — ровно SHA-256 токена; хэш — bcrypt стоимости схемы; версия перца — текущая.
    assert _query(lab.owner, "SELECT c.email_norm, left(c.password_hash, 7), c.pepper_version, s.token_sha256 "
                             "FROM customers c JOIN customer_sessions s ON s.customer_id = c.id") \
        == [(EMAIL, "$2a$04$", 1, created.session.sha256)]
    assert service.register(EMAIL, NEW_PASSWORD) == SessionOutcome("email_taken")

    login = service.login(EMAIL, PASSWORD, previous_session=created.session)       # NFC: тот же пароль
    assert login.result == "ok" and login.session is not None
    assert (service.resolve(login.session), service.resolve(created.session)) == (True, False)
    assert service.login(EMAIL, WRONG_PASSWORD) == SessionOutcome("invalid")
    assert service.login("nobody@example.test", PASSWORD) == SessionOutcome("invalid")

    # Сессия ДРУГОГО покупателя в previous_session не отзывается.
    other = service.register(OTHER, PASSWORD)
    assert service.login(EMAIL, PASSWORD, previous_session=other.session).result == "ok"
    assert service.resolve(other.session) is True

    for secret in (PASSWORD, unicodedata.normalize("NFD", PASSWORD), password_proof(PEPPER_1, PASSWORD),
                   created.session.value, login.session.value, other.session.value):
        assert _tables_containing(lab.owner, secret) == []
    assert _tables_containing(lab.owner, EMAIL) == ["customers"]                    # в аудит email не пишется


def test_refusals_are_committed_and_throttle_parses_retry_after(lab):
    service = lab.service()
    registered = service.register(EMAIL, PASSWORD)
    _run(lab.owner, "UPDATE auth_settings SET login_limit = 2")
    for _ in range(2):
        assert service.login(EMAIL, WRONG_PASSWORD) == SessionOutcome("invalid")
    # Видно другому соединению — значит, отказ зафиксирован, а не откачен.
    assert _query(lab.owner, "SELECT attempts FROM auth_throttle WHERE scope = 1") == [(2,)]
    throttled = service.login(EMAIL, PASSWORD)
    assert throttled.result == "throttled" and throttled.session is None
    assert type(throttled.retry_after_seconds) is int and 1 <= throttled.retry_after_seconds <= 15 * 60

    assert service.change_password(registered.session, WRONG_PASSWORD, NEW_PASSWORD) == SessionOutcome("invalid")
    assert _query(lab.owner, "SELECT attempts FROM auth_throttle WHERE scope = 2") == [(1,)]


def test_pepper_rotation_through_the_service(lab):
    registered = lab.service(SETTINGS).register(EMAIL, PASSWORD)
    assert _query(lab.owner, "SELECT pepper_version, auth_epoch FROM customers") == [(1, 1)]

    rotating = lab.service(ROTATING)
    login = rotating.login(EMAIL, PASSWORD)                    # доказательство v1 подошло, хэш пересчитан под v2
    assert login.result == "ok"
    assert _query(lab.owner, "SELECT pepper_version, auth_epoch FROM customers") == [(2, 1)]
    assert _query(lab.owner, "SELECT count(*) FROM audit_log WHERE action = 'auth.login' "
                             "AND (details->>'rehashed')::boolean") == [(1,)]
    assert rotating.resolve(registered.session) is True        # пересчёт эпоху не меняет

    assert lab.service(ROTATED).login(EMAIL, PASSWORD).result == "ok"
    # Нужной версии перца нет среди переданных: invalid и одна запись аудита за окно.
    assert lab.service(SETTINGS).login(EMAIL, PASSWORD) == SessionOutcome("invalid")
    assert lab.service(UNKNOWN_VERSION).login(EMAIL, PASSWORD) == SessionOutcome("invalid")
    assert _audit(lab.owner, "auth.pepper_missing") == 1


def test_profile_orders_and_order_views(lab, product):
    service = lab.service()
    mine = service.register(EMAIL, PASSWORD).session
    foreign = service.register(OTHER, PASSWORD).session
    profile = service.profile(mine)
    assert set(profile) == {"email", "created_at"} and profile["email"] == EMAIL
    assert isinstance(profile["created_at"], str)              # JSONB: метка времени строкой
    assert service.orders(mine) == []

    with lab.owner.cursor() as cur:
        order_id = _new_order(cur, product)
        assert _link(cur, mine.sha256, order_id) == {"result": "linked"}
    lab.owner.commit()

    orders = service.orders(mine)
    assert [o["id"] for o in orders] == [order_id]
    assert set(orders[0]) == {"id", "status", "created_at", "total_minor", "currency"}
    detail = service.order(mine, order_id)
    assert detail["order"]["id"] == order_id and len(detail["items"]) == 1
    assert set(detail["items"][0]) == {"product_id", "name", "product_type", "quantity", "unit_price_minor",
                                       "line_total_minor"}
    assert "token" not in str(detail) and "access" not in str(detail)
    # Чужой, несуществующий заказ и недействительная сессия — одинаковый None.
    assert service.order(foreign, order_id) is None and service.orders(foreign) == []
    assert service.order(mine, 2_147_483_647) is None
    service.logout(mine)
    assert (service.profile(mine), service.orders(mine), service.order(mine, order_id)) == (None, None, None)


def test_change_password_revokes_every_session_and_issues_a_new_one(lab):
    service = lab.service()
    registered = service.register(EMAIL, PASSWORD)
    second = service.login(EMAIL, PASSWORD)
    changed = service.change_password(second.session, PASSWORD, NEW_PASSWORD)
    assert changed.result == "ok" and changed.session.sha256 != second.session.sha256
    assert [service.resolve(s) for s in (registered.session, second.session, changed.session)] == [False, False, True]
    assert _query(lab.owner, "SELECT auth_epoch FROM customers") == [(2,)]
    assert service.login(EMAIL, PASSWORD) == SessionOutcome("invalid")
    assert service.login(EMAIL, NEW_PASSWORD).result == "ok"
    assert service.change_password(second.session, NEW_PASSWORD, PASSWORD) == SessionOutcome("session_invalid")
    assert _audit(lab.owner, "auth.password_change") == 1


def test_logout_logout_all_and_touch(lab):
    service = lab.service()
    registered = service.register(EMAIL, PASSWORD)
    first = service.login(EMAIL, PASSWORD)
    second = service.login(EMAIL, PASSWORD)
    assert service.touch(first.session) is False               # интервал продления (10 мин) не прошёл

    _run(lab.owner, "UPDATE auth_settings SET session_touch_interval = INTERVAL '0'")
    idle_sql = "SELECT idle_expires_at FROM customer_sessions WHERE token_sha256 = %s"
    idle_before = _query(lab.owner, idle_sql, (first.session.sha256,))[0][0]
    assert service.touch(first.session) is True
    assert _query(lab.owner, idle_sql, (first.session.sha256,))[0][0] >= idle_before

    assert service.logout(first.session) is None
    assert service.resolve(first.session) is False
    assert service.logout(first.session) is None                # идемпотентно
    assert service.logout(SessionToken.new()) is None
    assert service.touch(first.session) is False                # отозванную не продлить

    assert service.logout_all(second.session) == "ok"
    assert [service.resolve(s) for s in (registered.session, second.session)] == [False, False]
    assert _query(lab.owner, "SELECT auth_epoch FROM customers") == [(2,)]
    assert service.logout_all(second.session) == "session_invalid"
    assert service.logout_all(SessionToken.new()) == "session_invalid"


def test_database_flags_off_refuse_without_losing_sessions(lab):
    service = lab.service()
    kept = service.register(EMAIL, PASSWORD)
    logged_out = service.login(EMAIL, PASSWORD)
    _run(lab.owner, "UPDATE auth_settings SET registration_open = FALSE")
    assert service.register(OTHER, PASSWORD) == SessionOutcome("closed")

    _run(lab.owner, "UPDATE auth_settings SET accounts_enabled = FALSE")
    throttle = _query(lab.owner, "SELECT scope, attempts FROM auth_throttle ORDER BY scope")
    assert service.register(OTHER, PASSWORD) == SessionOutcome("disabled")
    assert service.login(EMAIL, PASSWORD) == SessionOutcome("disabled")
    assert service.change_password(kept.session, PASSWORD, NEW_PASSWORD) == SessionOutcome("disabled")
    assert service.logout_all(kept.session) == "disabled"
    assert (service.resolve(kept.session), service.touch(kept.session)) == (False, False)
    assert (service.profile(kept.session), service.orders(kept.session), service.order(kept.session, 1)) \
        == (None, None, None)
    assert service.logout(logged_out.session) is None           # выход работает и при выключенных аккаунтах
    assert _query(lab.owner, "SELECT scope, attempts FROM auth_throttle ORDER BY scope") == throttle

    _run(lab.owner, "UPDATE auth_settings SET accounts_enabled = TRUE")
    assert (service.resolve(kept.session), service.resolve(logged_out.session)) == (True, False)


# ---------- транзакции ----------

def test_each_operation_is_one_closed_read_committed_connection(lab):
    """Сеанс по умолчанию в REPEATABLE READ (функции 0005 там отказывают): сервис сам выставляет READ COMMITTED."""
    lab.session_setup.append("SET default_transaction_isolation = 'repeatable read'")
    service = lab.service()
    assert service.register(EMAIL, PASSWORD).result == "created"
    login = service.login(EMAIL, PASSWORD)
    assert login.result == "ok"
    assert service.resolve(login.session) is True
    service.touch(login.session)
    assert service.profile(login.session)["email"] == EMAIL
    assert service.orders(login.session) == []
    assert service.order(login.session, 1) is None
    changed = service.change_password(login.session, PASSWORD, NEW_PASSWORD)
    assert changed.result == "ok"
    assert service.logout_all(changed.session) == "ok"
    service.logout(changed.session)

    assert len(lab.opened) == 10                                # одна операция — одно соединение
    for conn, _ in lab.opened:
        assert conn.closed and conn.isolation_level == psycopg.IsolationLevel.READ_COMMITTED
    probe = lab.connect()
    assert _query(probe, "SHOW default_transaction_isolation") == [("repeatable read",)]
    probe.close()


def test_raise_in_a_function_is_internal_and_rolled_back(lab):
    """Настоящий RAISE (P0001) — «внутренняя ошибка»: данные, которые сервис уже проверил, база не приняла.
    Публичные методы такой вызов не построят (токен всегда 32 байта), поэтому — обёртка транзакции _call."""
    service = lab.service()
    failure = _service_error(lambda: service._call(SQL_RESOLVE, (bytes(31),), lambda value: True), "internal")
    assert failure.http_status == 500 and failure.diagnostic == "RaiseException:P0001"
    assert "SELECT" not in str(failure) and "байт" not in str(failure)     # ни SQL, ни текста RAISE
    assert failure.__cause__ is None
    assert lab.opened[-1][0].closed


def test_error_after_partial_work_rolls_back_the_whole_operation(lab, monkeypatch):
    """Вставка покупателя прошла, вставка сессии нарушила уникальность (тот же токен): откатывается всё."""
    service = lab.service(slots=1)
    first = service.register(EMAIL, PASSWORD)
    before = _state(lab.owner)
    monkeypatch.setattr(SessionToken, "new", classmethod(lambda cls: SessionToken(first.session.value)))
    failure = _service_error(lambda: service.register(OTHER, PASSWORD), "retry")
    monkeypatch.undo()
    assert failure.diagnostic == "UniqueViolation:23505"
    assert _state(lab.owner) == before
    assert service.register(OTHER, PASSWORD).result == "created"           # место bcrypt освобождено


def test_busy_while_customer_is_locked_is_a_committed_value_without_an_attempt(lab, peers):
    service = lab.service()
    service.register(EMAIL, PASSWORD)
    assert service.login(EMAIL, WRONG_PASSWORD) == SessionOutcome("invalid")
    before = _state(lab.owner)
    holder = _lock_customer(peers)
    started = time.monotonic()
    assert service.login(EMAIL, PASSWORD) == SessionOutcome("busy")       # lock_timeout функции — 2 с
    assert 1.5 <= time.monotonic() - started < 5
    holder.rollback()
    assert _state(lab.owner) == before                                    # учёт попытки откатан с подтранзакцией
    assert service.login(EMAIL, PASSWORD).result == "ok"


def test_statement_timeout_while_waiting_is_retry_and_commits_nothing(lab, peers):
    """Г2 A7 через сервис: statement_timeout сеанса короче lock_timeout функции."""
    service = lab.service(slots=1)
    service.register(EMAIL, PASSWORD)
    before = _state(lab.owner)
    holder = _lock_customer(peers)
    lab.session_setup.append("SET statement_timeout = '300ms'")
    failure = _service_error(lambda: service.login(EMAIL, PASSWORD), "retry")
    lab.session_setup.clear()
    holder.rollback()
    assert failure.http_status == 503 and failure.diagnostic == "QueryCanceled:57014"
    assert lab.opened[-1][0].closed
    assert _state(lab.owner) == before
    assert service.login(EMAIL, PASSWORD).result == "ok"                  # место bcrypt освобождено


def test_bcrypt_slot_is_refused_while_the_database_call_waits(lab, peers):
    service = lab.service(slots=1)
    service.register(EMAIL, PASSWORD)
    holder = _lock_customer(peers)
    opened = len(lab.opened)
    waiting = _Background(lambda: service.login(EMAIL, PASSWORD))
    _wait_blocked(lab.owner, _wait_new_connection(lab, opened))
    failure = _service_error(lambda: service.register(OTHER, PASSWORD), "overloaded")
    assert failure.http_status == 503 and len(lab.opened) == opened + 1   # к базе не обращались
    holder.rollback()                                                     # раньше lock_timeout (2 с)
    waiting.join()
    assert waiting.error is None, repr(waiting.error)
    assert waiting.result.result == "ok"
    assert service.register(OTHER, PASSWORD).result == "created"          # место освобождено


# ---------- неизвестный исход COMMIT (REV5 §6.2, I2) ----------

def _terminate_then_commit(observer):
    """Сервер завершает процесс соединения до COMMIT: настоящая ошибка psycopg, транзакция не зафиксирована."""
    def hook(conn):
        assert _query(observer, "SELECT pg_terminate_backend(%s, 5000)", (conn.info.backend_pid,)) == [(True,)]
        conn.commit()
    return hook


def _commit_then_lose_ack(conn):
    """COMMIT выполнен, ответ до приложения «не дошёл»."""
    conn.commit()
    raise psycopg.OperationalError("pytest: ответ на COMMIT потерян")


COMMITTING = {
    "register": lambda service, session: service.register(EMAIL, PASSWORD),
    "login": lambda service, session: service.login(EMAIL, PASSWORD),
    "change_password": lambda service, session: service.change_password(session, PASSWORD, NEW_PASSWORD),
}


@pytest.mark.parametrize("operation", COMMITTING)
def test_commit_failure_on_the_server_is_retry_after_exactly_one_resolve(lab, operation):
    service = lab.service()
    registered = service.register(EMAIL, PASSWORD) if operation != "register" else None
    before = _state(lab.owner)
    lab.commit_hooks.append(_terminate_then_commit(lab.owner))
    opened = len(lab.opened)
    failure = _service_error(lambda: COMMITTING[operation](service, registered and registered.session), "retry")
    assert failure.diagnostic == "commit_unknown"
    assert len(lab.opened) == opened + 2                        # операция и один resolve; повтора нет
    assert all(conn.closed for conn, _ in lab.opened)
    assert _state(lab.owner) == before                          # ни покупателя, ни сессии, ни учёта попытки
    if registered is not None:
        assert service.resolve(registered.session) is True


@pytest.mark.parametrize("operation", COMMITTING)
def test_lost_commit_acknowledgement_is_confirmed_by_exactly_one_resolve(lab, operation):
    service = lab.service()
    registered = service.register(EMAIL, PASSWORD) if operation != "register" else None
    lab.commit_hooks.append(_commit_then_lose_ack)
    opened = len(lab.opened)
    outcome = COMMITTING[operation](service, registered and registered.session)
    assert outcome.result in ("created", "ok") and outcome.session is not None
    assert len(lab.opened) == opened + 2
    assert service.resolve(outcome.session) is True
    if operation == "change_password":
        assert service.resolve(registered.session) is False


# ---------- контракт email: что сервис пропустил, база не должна отвергать ----------

EMAIL_PROBES = (
    EMAIL, "a+tag@sub.example.test", "покупатель@пример.рф", "straße@example.test", "a\x01b@example.test",
    "x" * 241 + "@example.test",                                 # 254 символа — предел
    "buyer@example.test\n",                                      # re.match + '$' в Python допускает '\n' в конце
)


def _service_accepts(email):
    try:
        check_email_norm(email)
    except AccountInputError:
        return False
    return True


def _sql_accepts(conn, email):
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT auth_check_email(%s), auth_check_not_reserved(%s)", (email, email))
    except psycopg.errors.RaiseException:
        conn.rollback()
        return False
    conn.rollback()
    return True


def test_every_email_the_service_accepts_is_accepted_by_sql(lab):
    """Иначе ввод покупателя доходит до RAISE в базе: 500 вместо 422 (и занимает место bcrypt)."""
    accepted = [email for email in EMAIL_PROBES if _service_accepts(email)]
    assert EMAIL in accepted
    assert [email for email in accepted if not _sql_accepts(lab.owner, email)] == []
