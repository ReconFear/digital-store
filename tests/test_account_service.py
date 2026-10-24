"""PR6 этап 2c: сервис аккаунтов (accounts/service.py) — офлайн, на поддельных соединениях.

Без PostgreSQL: фабрика соединений сервиса подменяется. Ошибки — настоящие классы исключений psycopg.
Проверяется: одна функция и явный COMMIT на операцию; ответ проверяется ДО фиксации; ROLLBACK при ошибках;
неизвестный исход COMMIT — ровно один resolve на новом соединении, без повтора операции; сессия выдаётся
только после подтверждённой фиксации; в SQL нет пароля и сырого токена; семафор bcrypt неблокирующий и
освобождается при любом исходе; при выключенных аккаунтах база не трогается.
"""

import ast
import re
import threading
from pathlib import Path

import psycopg
import pytest

from accounts.config import AccountSettings, Pepper
from accounts.proof import PasswordPolicyError, password_proof
from accounts.service import (
    SQL_LOGIN, SQL_ORDER, SQL_ORDERS, SQL_PASSWORD_CHANGE, SQL_PROFILE, SQL_REGISTER, SQL_RESOLVE, SQL_REVOKE,
    SQL_REVOKE_ALL, SQL_TOUCH, _EMAIL_RE, AccountInputError, AccountService, AccountServiceError, SessionOutcome,
    check_email_norm,
)
from accounts.session import SessionToken

PEPPER_1 = Pepper(1, bytes(range(32)))
PEPPER_2 = Pepper(2, bytes(range(32, 64)))
SETTINGS = AccountSettings(enabled=True, pepper_current=PEPPER_2, pepper_previous=PEPPER_1,
                           csrf_key=bytes(range(64, 96)))
EMAIL = "buyer@example.test"
PASSWORD = "Секретный пароль 2026"
NEW_PASSWORD = "Новый секретный пароль 2027"


# ---------- поддельные соединения ----------

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))
        if self.conn.started is not None:
            self.conn.started.set()
        if self.conn.release is not None:
            assert self.conn.release.wait(10), "тест не отпустил соединение"
        if self.conn.execute_error is not None:
            raise self.conn.execute_error

    def fetchone(self):
        return (self.conn.result,)


class FakeConnection:
    def __init__(self, result=None, execute_error=None, commit_error=None, rollback_error=None, release=None):
        self.result = result
        self.execute_error = execute_error
        self.commit_error = commit_error
        self.rollback_error = rollback_error
        self.release = release
        self.started = threading.Event() if release is not None else None
        self.isolation_level = None
        self.executed, self.commits, self.rollbacks, self.closed = [], 0, 0, False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        if self.commit_error is not None:
            raise self.commit_error

    def rollback(self):
        self.rollbacks += 1
        if self.rollback_error is not None:
            raise self.rollback_error

    def close(self):
        self.closed = True


class Factory:
    """Выдаёт заранее заданные соединения по порядку; Exception в списке — ошибка подключения."""

    def __init__(self, *connections):
        self.queue = list(connections)
        self.opened = []

    def __call__(self):
        assert self.queue, "сервис открыл больше соединений, чем ожидалось"
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        self.opened.append(item)
        return item


def _service(*connections, settings=SETTINGS, slots=2):
    factory = Factory(*connections)
    return AccountService(settings, connect=factory, bcrypt_slots=slots), factory


def _all_params(factory):
    return [p for conn in factory.opened for _, params in conn.executed for p in (params or ())]


def _assert_one_committed_call(conn, sql):
    assert [s for s, _ in conn.executed] == [sql]
    assert (conn.commits, conn.rollbacks, conn.closed) == (1, 0, True)
    assert conn.isolation_level == psycopg.IsolationLevel.READ_COMMITTED


def _service_error(fn, *args, code):
    with pytest.raises(AccountServiceError) as error:
        fn(*args)
    assert error.value.code == code
    return error.value


# ---------- регистрация, вход, смена пароля ----------

def test_register_sends_only_proof_and_session_hash_and_commits():
    conn = FakeConnection({"result": "created"})
    service, factory = _service(conn)
    outcome = service.register(EMAIL, PASSWORD)
    _assert_one_committed_call(conn, SQL_REGISTER)
    email, proof, version, session_hash = conn.executed[0][1]
    assert (email, proof, version) == (EMAIL, password_proof(PEPPER_2, PASSWORD), 2)
    assert outcome == SessionOutcome("created", session=outcome.session)
    assert session_hash == outcome.session.sha256
    assert PASSWORD not in map(str, _all_params(factory)) and outcome.session.value not in map(str, _all_params(factory))


@pytest.mark.parametrize("result", ["email_taken", "busy", "disabled", "closed"])
def test_register_refusals_are_committed_without_session(result):
    conn = FakeConnection({"result": result})
    service, _ = _service(conn)
    assert service.register(EMAIL, PASSWORD) == SessionOutcome(result)
    _assert_one_committed_call(conn, SQL_REGISTER)


def test_login_passes_current_and_previous_proofs_and_previous_session():
    conn = FakeConnection({"result": "ok"})
    service, factory = _service(conn)
    previous = SessionToken.new()
    outcome = service.login(EMAIL, PASSWORD, previous_session=previous)
    _assert_one_committed_call(conn, SQL_LOGIN)
    assert conn.executed[0][1] == (EMAIL, password_proof(PEPPER_2, PASSWORD), 2, password_proof(PEPPER_1, PASSWORD), 1,
                                   outcome.session.sha256, previous.sha256)
    assert outcome.result == "ok" and outcome.session is not None
    assert previous.value not in map(str, _all_params(factory))


def test_login_without_previous_pepper_or_session():
    conn = FakeConnection({"result": "ok"})
    service, _ = _service(conn, settings=AccountSettings(enabled=True, pepper_current=PEPPER_1,
                                                         csrf_key=bytes(range(64, 96))))
    service.login(EMAIL, PASSWORD)
    params = conn.executed[0][1]
    assert params[1:5] == (password_proof(PEPPER_1, PASSWORD), 1, None, None) and params[6] is None


@pytest.mark.parametrize("value, expected", [
    ({"result": "invalid"}, SessionOutcome("invalid")),
    ({"result": "busy"}, SessionOutcome("busy")),
    ({"result": "disabled"}, SessionOutcome("disabled")),
    ({"result": "throttled", "retry_after_seconds": 120}, SessionOutcome("throttled", retry_after_seconds=120)),
])
def test_login_refusals_are_committed_so_the_attempt_counts(value, expected):
    conn = FakeConnection(value)
    service, _ = _service(conn)
    assert service.login(EMAIL, PASSWORD) == expected
    _assert_one_committed_call(conn, SQL_LOGIN)


def test_change_password_uses_one_new_proof_and_a_new_session():
    conn = FakeConnection({"result": "ok"})
    service, factory = _service(conn)
    session = SessionToken.new()
    outcome = service.change_password(session, PASSWORD, NEW_PASSWORD)
    _assert_one_committed_call(conn, SQL_PASSWORD_CHANGE)
    assert conn.executed[0][1] == (session.sha256, password_proof(PEPPER_2, PASSWORD), 2,
                                   password_proof(PEPPER_1, PASSWORD), 1, password_proof(PEPPER_2, NEW_PASSWORD), 2,
                                   outcome.session.sha256)
    assert outcome.session.sha256 != session.sha256
    texts = list(map(str, _all_params(factory)))
    assert PASSWORD not in texts and NEW_PASSWORD not in texts


@pytest.mark.parametrize("result", ["invalid", "session_invalid", "busy", "disabled"])
def test_change_password_refusals_have_no_session(result):
    conn = FakeConnection({"result": result})
    service, _ = _service(conn)
    assert service.change_password(SessionToken.new(), PASSWORD, NEW_PASSWORD) == SessionOutcome(result)
    assert conn.commits == 1


# ---------- неизвестный исход COMMIT ----------

@pytest.mark.parametrize("method, args, sql, success", [
    ("register", (EMAIL, PASSWORD), SQL_REGISTER, "created"),
    ("login", (EMAIL, PASSWORD), SQL_LOGIN, "ok"),
    ("change_password", (SessionToken.new(), PASSWORD, NEW_PASSWORD), SQL_PASSWORD_CHANGE, "ok"),
])
def test_unknown_commit_resolved_once_on_a_new_connection(method, args, sql, success):
    first = FakeConnection({"result": success}, commit_error=psycopg.OperationalError("connection lost"))
    second = FakeConnection({"valid": True})
    service, factory = _service(first, second)
    outcome = getattr(service, method)(*args)
    assert outcome.result == success and outcome.session is not None
    assert [s for s, _ in first.executed] == [sql] and [s for s, _ in second.executed] == [SQL_RESOLVE]
    assert second.executed[0][1] == (outcome.session.sha256,)
    assert first.closed and second.closed and factory.queue == []


@pytest.mark.parametrize("resolve", [
    FakeConnection({"valid": False}),
    psycopg.OperationalError("database is down"),
    FakeConnection(execute_error=psycopg.errors.QueryCanceled("timeout")),
    FakeConnection({"unexpected": True}),
])
def test_unknown_commit_not_confirmed_means_retry_without_repeating_the_operation(resolve):
    first = FakeConnection({"result": "created"}, commit_error=psycopg.OperationalError("connection lost"))
    service, factory = _service(first, resolve)
    error = _service_error(service.register, EMAIL, PASSWORD, code="retry")
    assert error.http_status == 503
    assert [s for conn in factory.opened for s, _ in conn.executed].count(SQL_REGISTER) == 1


@pytest.mark.parametrize("value", [{"result": "invalid"}, {"result": "throttled", "retry_after_seconds": 5},
                                   {"result": "busy"}])
def test_unknown_commit_of_a_refusal_is_retry_without_resolve(value):
    """REV5 I2: при сбое COMMIT — 503 для любого результата; результат, учёт которого мог не зафиксироваться,
    клиенту не сообщается."""
    first = FakeConnection(value, commit_error=psycopg.OperationalError("connection lost"))
    service, factory = _service(first)
    _service_error(service.login, EMAIL, PASSWORD, code="retry")
    assert len(factory.opened) == 1


@pytest.mark.parametrize("method, args, value", [
    ("logout_all", (SessionToken.new(),), {"result": "ok"}),
    ("logout", (SessionToken.new(),), {"result": "ok"}),
    ("resolve", (SessionToken.new(),), {"valid": True}),
    ("touch", (SessionToken.new(),), {"touched": True}),
    ("profile", (SessionToken.new(),), {"email": EMAIL}),
])
def test_unknown_commit_of_other_operations_is_retry_and_not_repeated(method, args, value):
    conn = FakeConnection(value, commit_error=psycopg.OperationalError("connection lost"))
    service, factory = _service(conn)
    _service_error(getattr(service, method), *args, code="retry")
    assert len(factory.opened) == 1 and len(conn.executed) == 1


# ---------- ошибки базы и контракта ----------

@pytest.mark.parametrize("error, code", [
    (psycopg.errors.QueryCanceled("canceling statement due to statement timeout"), "retry"),
    (psycopg.OperationalError("server closed the connection"), "retry"),
    (psycopg.errors.SerializationFailure("could not serialize"), "retry"),
    (psycopg.errors.DeadlockDetected("deadlock"), "retry"),
    (psycopg.errors.LockNotAvailable("lock timeout"), "retry"),
    (psycopg.errors.RaiseException("неверный формат"), "internal"),
    (psycopg.errors.UndefinedFunction("function does not exist"), "retry"),
])
def test_database_errors_roll_back_and_reveal_nothing(error, code):
    conn = FakeConnection(execute_error=error)
    service, factory = _service(conn)
    failure = _service_error(service.login, EMAIL, PASSWORD, code=code)
    assert (conn.commits, conn.rollbacks, conn.closed) == (0, 1, True)
    assert failure.http_status == {"retry": 503, "internal": 500}[code]
    assert failure.diagnostic.startswith(type(error).__name__ + ":")
    for text in (str(failure), repr(failure), failure.diagnostic):
        assert PASSWORD not in text and "SELECT" not in text and str(error) not in text
    assert failure.__cause__ is None and failure.__suppress_context__


def test_connection_failure_is_retry_and_opens_nothing_else():
    service, factory = _service(psycopg.OperationalError("connection refused"))
    _service_error(service.register, EMAIL, PASSWORD, code="retry")


def test_failed_rollback_does_not_mask_the_original_error():
    conn = FakeConnection(execute_error=psycopg.errors.QueryCanceled("timeout"),
                          rollback_error=psycopg.OperationalError("broken"))
    service, _ = _service(conn)
    _service_error(service.login, EMAIL, PASSWORD, code="retry")
    assert conn.closed


@pytest.mark.parametrize("method, args, value", [
    ("register", (EMAIL, PASSWORD), {"result": "ok"}),                    # не результат регистрации
    ("register", (EMAIL, PASSWORD), None),
    ("login", (EMAIL, PASSWORD), {"result": "created"}),
    ("login", (EMAIL, PASSWORD), {"result": "throttled"}),               # без retry_after_seconds
    ("login", (EMAIL, PASSWORD), {"result": "throttled", "retry_after_seconds": True}),
    ("login", (EMAIL, PASSWORD), "ok"),
    ("logout_all", (SessionToken.new(),), {"result": "created"}),
    ("logout", (SessionToken.new(),), {"result": "busy"}),
    ("resolve", (SessionToken.new(),), {"valid": "yes"}),
    ("touch", (SessionToken.new(),), {}),
    ("profile", (SessionToken.new(),), ["not", "a", "dict"]),
    ("orders", (SessionToken.new(),), {"not": "a list"}),
])
def test_answers_outside_the_contract_are_rolled_back_not_committed(method, args, value):
    conn = FakeConnection(value)
    service, _ = _service(conn)
    _service_error(getattr(service, method), *args, code="internal")
    assert (conn.commits, conn.rollbacks, conn.closed) == (0, 1, True)


# ---------- семафор bcrypt ----------

def test_bcrypt_semaphore_refuses_without_touching_the_database_and_is_released():
    release = threading.Event()
    blocked = FakeConnection({"result": "invalid"}, release=release)
    service, factory = _service(blocked, FakeConnection({"valid": True}), FakeConnection({"result": "ok"}), slots=1)

    holder = threading.Thread(target=service.login, args=(EMAIL, PASSWORD), daemon=True)
    holder.start()
    assert blocked.started.wait(5)
    error = _service_error(service.register, EMAIL, PASSWORD, code="overloaded")
    assert error.http_status == 503 and len(factory.opened) == 1      # к базе не обращались
    # Операции без bcrypt семафор не занимают.
    assert service.resolve(SessionToken.new()) is True
    release.set()
    holder.join(5)
    assert not holder.is_alive()
    assert service.login(EMAIL, PASSWORD).result == "ok"                # место освободилось


@pytest.mark.parametrize("first", [
    FakeConnection(execute_error=psycopg.errors.QueryCanceled("timeout")),
    FakeConnection({"result": "unexpected"}),
    FakeConnection({"result": "ok"}, commit_error=psycopg.OperationalError("lost")),
    psycopg.OperationalError("connection refused"),
])
def test_bcrypt_slot_is_released_after_any_failure(first):
    queue = [first]
    if isinstance(first, FakeConnection) and first.commit_error is not None:
        queue.append(FakeConnection({"valid": False}))
    queue.append(FakeConnection({"result": "ok"}))
    service, _ = _service(*queue, slots=1)
    with pytest.raises(AccountServiceError):
        service.login(EMAIL, PASSWORD)
    assert service.login(EMAIL, PASSWORD).result == "ok"


def test_password_policy_error_needs_no_slot_and_no_connection():
    service, factory = _service(FakeConnection({"result": "ok"}), slots=1)
    with pytest.raises(PasswordPolicyError):
        service.register(EMAIL, "short")
    assert factory.opened == []
    assert service.login(EMAIL, PASSWORD).result == "ok"               # единственный слот свободен


@pytest.mark.parametrize("slots", [0, -1, True, 1.5, "2"])
def test_invalid_slot_count(slots):
    with pytest.raises(ValueError):
        AccountService(SETTINGS, connect=Factory(), bcrypt_slots=slots)


# ---------- входные данные, флаг, просмотр ----------

@pytest.mark.parametrize("email, code", [
    ("Buyer@example.test", "email_invalid"), ("buyer", "email_invalid"), ("a b@example.test", "email_invalid"),
    ("a@b@c", "email_invalid"), ("x" * 250 + "@a.bc", "email_invalid"), (None, "email_invalid"),
    ("released+1@invalid", "email_reserved"), ("buyer@shop.invalid", "email_reserved"),
    # База отвергает (RAISE → было бы 500) или psycopg не передаёт (DataError → 503, UnicodeEncodeError).
    ("buyer@example.test\n", "email_invalid"), ("a\x00b@example.test", "email_invalid"),
    ("a\ud800b@example.test", "email_invalid"), ("buyer@example.test\udfff", "email_invalid"),
])
def test_email_must_match_the_database_contract_before_any_connection(email, code):
    service, factory = _service()
    for call in (lambda: service.register(email, PASSWORD), lambda: service.login(email, PASSWORD)):
        with pytest.raises(AccountInputError) as error:
            call()
        assert error.value.code == code
        assert str(error.value) == code and error.value.__cause__ is None
    assert factory.opened == []


def test_email_pattern_is_the_database_pattern_applied_to_the_whole_string():
    migration = Path(__file__).resolve().parent.parent / "migrations" / "0005_customer_auth.sql"
    sql = migration.read_text(encoding="utf-8-sig")
    check = sql[sql.index("CREATE FUNCTION auth_check_email("):]
    sql_pattern = re.search(r"p_email_norm !~ '([^']+)'", check[:check.index("$$;")]).group(1)
    assert sql_pattern == "^" + _EMAIL_RE.pattern + "$"
    assert check_email_norm("a+tag@sub.example.test") == "a+tag@sub.example.test"
    assert check_email_norm("x" * 241 + "@example.test") == "x" * 241 + "@example.test"     # 254 символа


def test_disabled_accounts_never_touch_the_database():
    service, factory = _service(settings=AccountSettings(enabled=False))
    session = SessionToken.new()
    for call in (lambda: service.register(EMAIL, PASSWORD), lambda: service.login(EMAIL, PASSWORD),
                 lambda: service.change_password(session, PASSWORD, NEW_PASSWORD), lambda: service.resolve(session),
                 lambda: service.touch(session), lambda: service.logout(session), lambda: service.logout_all(session),
                 lambda: service.profile(session), lambda: service.orders(session), lambda: service.order(session, 1)):
        error = _service_error(call, code="disabled")
        assert error.http_status == 404
    assert factory.opened == []


@pytest.mark.parametrize("method, args, sql, value, expected", [
    ("resolve", (), SQL_RESOLVE, {"valid": False}, False),
    ("touch", (), SQL_TOUCH, {"touched": True}, True),
    ("logout_all", (), SQL_REVOKE_ALL, {"result": "session_invalid"}, "session_invalid"),
    ("logout", (), SQL_REVOKE, {"result": "ok"}, None),
    ("profile", (), SQL_PROFILE, {"email": EMAIL, "created_at": "2026-10-17"}, {"email": EMAIL,
                                                                                "created_at": "2026-10-17"}),
    ("profile", (), SQL_PROFILE, None, None),
    ("orders", (), SQL_ORDERS, [{"id": 7}], [{"id": 7}]),
    ("orders", (), SQL_ORDERS, None, None),
    ("order", (7,), SQL_ORDER, {"order": {"id": 7}}, {"order": {"id": 7}}),
    ("order", (7,), SQL_ORDER, None, None),
])
def test_session_operations_commit_one_call(method, args, sql, value, expected):
    conn = FakeConnection(value)
    service, _ = _service(conn)
    session = SessionToken.new()
    assert getattr(service, method)(session, *args) == expected
    _assert_one_committed_call(conn, sql)
    assert conn.executed[0][1][0] == session.sha256


@pytest.mark.parametrize("order_id", [0, -1, 2 ** 31, True, "7", None])
def test_order_with_impossible_id_is_not_found_without_a_query(order_id):
    service, factory = _service()
    assert service.order(SessionToken.new(), order_id) is None
    assert factory.opened == []


def test_session_arguments_must_be_session_tokens():
    service, _ = _service()
    with pytest.raises(TypeError):
        service.resolve("A" * 43)
    with pytest.raises(TypeError):
        service.login(EMAIL, PASSWORD, previous_session="A" * 43)


def test_reprs_hide_secrets():
    conn = FakeConnection({"result": "ok"})
    service, _ = _service(conn)
    outcome = service.login(EMAIL, PASSWORD)
    text = repr(service) + repr(outcome) + str(outcome)
    assert outcome.session.value not in text and PASSWORD not in text


def test_service_module_does_not_import_database_at_import_time():
    """database.py при импорте читает .env: сервис импортирует его только внутри фабрики по умолчанию."""
    tree = ast.parse((Path(__file__).resolve().parent.parent / "accounts" / "service.py").read_text(encoding="utf-8"))
    top_level = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
    top_level |= {node.module for node in tree.body if isinstance(node, ast.ImportFrom)}
    assert "database" not in top_level and "cors_settings" not in top_level
