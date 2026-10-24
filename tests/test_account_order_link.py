"""PR6 этап 2e-1: привязка нового заказа к аккаунту (accounts/order_link.py) и шаг after_access в
payments.access.create_order_with_access — офлайн, без PostgreSQL.

Порядок шагов транзакции проверяется на НАСТОЯЩЕМ commerce.orders.create_order: подменяется только
database.get_connection — поддельное соединение ведёт себя как psycopg 3 (выход из `with conn` — COMMIT,
исключение — ROLLBACK) и записывает каждый шаг. Так видно: order_access_issue → customer_order_link_new →
COMMIT на одном курсоре, а любой отказ привязки — ROLLBACK без COMMIT.
"""

import ast
import hashlib
import os
import subprocess
import sys
from pathlib import Path

import psycopg
import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from accounts.config import AccountSettings, Pepper
from accounts.order_link import (
    SQL_LINK_NEW_ORDER, OrderLinkError, OrderLinkRefused, link_new_order, order_session,
)
from accounts.request_guard import OriginAllowlist, RequestRefused, csrf_token
from accounts.session import SESSION_COOKIE_NAME, SessionToken

# payments.access -> commerce.orders -> database.py, который при импорте вызывает load_dotenv(). На время импорта
# чтение .env выключено (python-dotenv 1.2: PYTHON_DOTENV_DISABLED); окружение затем восстанавливается.
_previous = os.environ.get("PYTHON_DOTENV_DISABLED")
os.environ["PYTHON_DOTENV_DISABLED"] = "1"
try:
    import database
    from payments import access
finally:
    if _previous is None:
        del os.environ["PYTHON_DOTENV_DISABLED"]
    else:
        os.environ["PYTHON_DOTENV_DISABLED"] = _previous

PROJECT_DIR = Path(__file__).resolve().parent.parent
SHOP = "https://shop.example.test"
TRUSTED = OriginAllowlist([SHOP])
CSRF_KEY = bytes(range(64, 96))
ENABLED = AccountSettings(enabled=True, pepper_current=Pepper(1, bytes(range(32))), csrf_key=CSRF_KEY)
ORDER_ID = 41
ITEMS = [(5, 2)]
PRODUCT = (5, "Ключ", 10000, "RUB", 10, True, "tracked", "software_key", "manual")


# ---------- поддельное соединение для настоящего create_order ----------

def _step(sql):
    for marker, step in (("FROM products", "SELECT products"), ("INSERT INTO orders", "INSERT orders"),
                         ("INSERT INTO order_items", "INSERT order_items"),
                         ("order_access_issue", "order_access_issue"),
                         ("customer_order_link_new", "customer_order_link_new")):
        if marker in sql:
            return step
    return sql


class _Connection:
    """Как psycopg 3: `with conn` — COMMIT при выходе, ROLLBACK при исключении, затем закрытие."""

    def __init__(self, link_answer=None, fail_on=None):
        self.link_answer = link_answer
        self.fail_on = fail_on
        self.log, self.calls = [], []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.log += ["ROLLBACK" if exc_type else "COMMIT", "CLOSE"]
        return False

    def cursor(self):
        return _Cursor(self)

    def params(self, step):
        return [params for name, _, params in self.calls if name == step]


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.last = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        step = _step(sql)
        self.conn.log.append(step)
        self.conn.calls.append((step, id(self), params))
        if step == self.conn.fail_on:
            raise psycopg.errors.RaiseException("отказ функции базы")
        self.last = step

    def fetchall(self):
        assert self.last == "SELECT products"
        return [PRODUCT]

    def fetchone(self):
        if self.last == "INSERT orders":
            return ORDER_ID, "2026-10-17T12:00:00+03:00"
        if self.last == "customer_order_link_new":
            return (self.conn.link_answer,)
        raise AssertionError(f"fetchone после {self.last}")


@pytest.fixture
def connection(monkeypatch):
    holder = {}

    def factory(**kwargs):
        conn = _Connection(**kwargs)
        monkeypatch.setattr(database, "get_connection", lambda: conn)
        holder["conn"] = conn
        return conn
    return factory


def _linker(session):
    return lambda cursor, order_id: link_new_order(cursor, session, order_id)


ORDER_STEPS = ["SELECT products", "INSERT orders", "INSERT order_items", "order_access_issue"]


# ---------- create_order_with_access: совместимость и порядок шагов ----------

def test_without_after_access_the_guest_contract_is_unchanged(connection):
    conn = connection()
    order, token = access.create_order_with_access(ITEMS)
    assert conn.log == ORDER_STEPS + ["COMMIT", "CLOSE"]                      # ни шага привязки, ни лишнего
    assert set(order) == {"id", "status", "created_at", "total", "items"} and order["id"] == ORDER_ID
    assert len(token) == 43
    assert conn.params("order_access_issue") == [(ORDER_ID, hashlib.sha256(token.encode()).hexdigest())]
    assert not any(token in str(params) for _, _, params in conn.calls)    # сам секрет в SQL не уходит
    parameters = list(__import__("inspect").signature(access.create_order_with_access).parameters.values())
    assert [(p.name, p.default) for p in parameters][1:] == [("after_access", None)]


def test_link_runs_after_access_on_the_same_cursor_before_commit(connection):
    conn = connection(link_answer={"result": "linked"})
    session = SessionToken.new()
    order, token = access.create_order_with_access(ITEMS, after_access=_linker(session))
    assert conn.log == ORDER_STEPS + ["customer_order_link_new", "COMMIT", "CLOSE"]
    cursors = {cursor for step, cursor, _ in conn.calls if step in ("order_access_issue", "customer_order_link_new")}
    assert len(cursors) == 1                                                 # один курсор — одна транзакция
    assert conn.params("customer_order_link_new") == [(session.sha256, ORDER_ID)]
    assert len(session.sha256) == 32
    assert not any(session.value in str(params) or token in str(params) for _, _, params in conn.calls)
    assert order["id"] == ORDER_ID


@pytest.mark.parametrize("result", ["session_invalid", "disabled"])
def test_link_refusal_rolls_the_whole_order_back(connection, result):
    conn = connection(link_answer={"result": result})
    with pytest.raises(OrderLinkRefused) as refused:
        access.create_order_with_access(ITEMS, after_access=_linker(SessionToken.new()))
    assert refused.value.result == result
    assert conn.log == ORDER_STEPS + ["customer_order_link_new", "ROLLBACK", "CLOSE"]   # гостевого заказа нет


@pytest.mark.parametrize("answer", [{"result": "ok"}, {"result": "linked", "customer_id": 1}, {"result": None}, {},
                                    None, "linked", ["linked"]])
def test_answer_outside_the_contract_rolls_back(connection, answer):
    conn = connection(link_answer=answer)
    with pytest.raises(OrderLinkError) as error:
        access.create_order_with_access(ITEMS, after_access=_linker(SessionToken.new()))
    assert "COMMIT" not in conn.log and conn.log[-2:] == ["ROLLBACK", "CLOSE"]
    assert "customer_id" not in str(error.value)


def test_any_error_in_after_access_or_in_the_database_rolls_back(connection):
    def broken(cursor, order_id):
        raise RuntimeError("сбой шага привязки")
    conn = connection()
    with pytest.raises(RuntimeError):
        access.create_order_with_access(ITEMS, after_access=broken)
    assert conn.log == ORDER_STEPS + ["ROLLBACK", "CLOSE"]

    conn = connection(fail_on="customer_order_link_new")                     # RAISE функции привязки
    with pytest.raises(psycopg.errors.RaiseException):
        access.create_order_with_access(ITEMS, after_access=_linker(SessionToken.new()))
    assert conn.log == ORDER_STEPS + ["customer_order_link_new", "ROLLBACK", "CLOSE"]

    called = []
    conn = connection(fail_on="order_access_issue")                          # секрет не выдан — привязки нет
    with pytest.raises(psycopg.errors.RaiseException):
        access.create_order_with_access(ITEMS, after_access=lambda cursor, order_id: called.append(order_id))
    assert called == [] and conn.log == ORDER_STEPS + ["ROLLBACK", "CLOSE"]


# ---------- link_new_order ----------

class _LinkCursor:
    def __init__(self, answer):
        self.answer, self.executed = answer, []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.answer


def test_link_sends_only_the_session_hash():
    session = SessionToken.new()
    cursor = _LinkCursor(({"result": "linked"},))
    assert link_new_order(cursor, session, 7) is None
    assert cursor.executed == [(SQL_LINK_NEW_ORDER, (session.sha256, 7))]
    assert SQL_LINK_NEW_ORDER == "SELECT customer_order_link_new(%s, %s)"
    assert session.value not in repr(cursor.executed)


def test_link_without_a_row_is_a_contract_error():
    with pytest.raises(OrderLinkError):
        link_new_order(_LinkCursor(None), SessionToken.new(), 7)


@pytest.mark.parametrize("session, order_id, error", [
    ("A" * 43, 7, TypeError), (None, 7, TypeError), (SessionToken.new(), 0, ValueError),
    (SessionToken.new(), -1, ValueError), (SessionToken.new(), True, ValueError), (SessionToken.new(), "7", ValueError),
    (SessionToken.new(), 2 ** 31, ValueError), (SessionToken.new(), None, ValueError),
])
def test_link_rejects_impossible_arguments_without_sql(session, order_id, error):
    cursor = _LinkCursor(({"result": "linked"},))
    with pytest.raises(error):
        link_new_order(cursor, session, order_id)
    assert cursor.executed == []


# ---------- order_session ----------

class _Request:
    def __init__(self, cookies=None, headers=(), cookies_forbidden=False):
        self._cookies = cookies or {}
        self._forbidden = cookies_forbidden
        self.headers = Headers(raw=[(name.lower().encode(), value.encode()) for name, value in headers])
        self.cookie_reads = 0

    @property
    def cookies(self):
        if self._forbidden:
            raise AssertionError("cookie прочитана при выключенных аккаунтах")
        self.cookie_reads += 1
        return self._cookies


def _account_request(session_value, *, origin=SHOP, content_type="application/json", csrf=None, extra=()):
    headers = [("Origin", origin)] if origin is not None else []
    headers += [("Content-Type", content_type)] if content_type is not None else []
    headers += [("X-CSRF-Token", csrf)] if csrf is not None else []
    return _Request({SESSION_COOKIE_NAME: session_value}, headers + list(extra))


def test_disabled_accounts_never_read_the_cookie():
    request = _Request(cookies_forbidden=True, headers=[("Origin", "https://evil.example.test")])
    assert order_session(request, AccountSettings(), None) is None


def test_request_without_a_session_cookie_is_a_guest_without_any_checks():
    for request in (_Request(), _Request({"other": "x"}, [("Origin", "https://evil.example.test")])):
        assert order_session(request, ENABLED, TRUSTED) is None
        assert request.cookie_reads == 1


def test_valid_cookie_origin_json_and_csrf_give_the_session():
    session = SessionToken.new()
    request = _account_request(session.value, csrf=csrf_token(CSRF_KEY, session),
                               content_type="application/json; charset=utf-8")
    found = order_session(request, ENABLED, TRUSTED)
    assert isinstance(found, SessionToken) and found.value == session.value


def test_real_starlette_request_is_parsed_the_same_way():
    session = SessionToken.new()
    scope = {"type": "http", "method": "POST", "path": "/api/v2/orders", "headers": [
        (b"origin", SHOP.encode()), (b"content-type", b"application/json"),
        (b"x-csrf-token", csrf_token(CSRF_KEY, session).encode()),
        (b"cookie", f"theme=dark; {SESSION_COOKIE_NAME}={session.value}".encode())]}
    assert order_session(Request(scope), ENABLED, TRUSTED).value == session.value


_OTHER = SessionToken.new()


@pytest.mark.parametrize("make", [
    lambda s: _account_request(s.value, origin=None, csrf=csrf_token(CSRF_KEY, s)),
    lambda s: _account_request(s.value, origin="https://evil.example.test", csrf=csrf_token(CSRF_KEY, s)),
    lambda s: _account_request(s.value, origin="null", csrf=csrf_token(CSRF_KEY, s)),
    lambda s: _account_request(s.value, content_type=None, csrf=csrf_token(CSRF_KEY, s)),
    lambda s: _account_request(s.value, content_type="text/plain", csrf=csrf_token(CSRF_KEY, s)),
    lambda s: _account_request(s.value),                                               # без CSRF-токена
    lambda s: _account_request(s.value, csrf="A" * 43),
    lambda s: _account_request(s.value, csrf=csrf_token(CSRF_KEY, _OTHER)),           # токен другой сессии
    lambda s: _account_request(s.value, csrf=csrf_token(bytes(32), s)),               # другой ключ
    lambda s: _account_request(s.value, csrf=csrf_token(CSRF_KEY, s),
                               extra=[("X-CSRF-Token", csrf_token(CSRF_KEY, s))]),     # два заголовка
    lambda s: _account_request("not-a-token", csrf=csrf_token(CSRF_KEY, s)),          # повреждённая cookie (В2)
    lambda s: _account_request("", csrf=csrf_token(CSRF_KEY, s)),
    lambda s: _account_request(s.value + "A", csrf=csrf_token(CSRF_KEY, s)),
])
def test_cookie_with_a_failed_check_is_refused_403(make):
    with pytest.raises(RequestRefused) as refused:
        order_session(make(SessionToken.new()), ENABLED, TRUSTED)
    assert (refused.value.http_status, refused.value.code) == (403, "csrf_failed")


def test_order_session_requires_settings_and_trusted_origins():
    with pytest.raises(TypeError):
        order_session(_Request(), None, TRUSTED)
    for trusted in (None, [SHOP], OriginAllowlist([])):
        with pytest.raises(TypeError):
            order_session(_Request(), ENABLED, trusted)


# ---------- модуль не обращается к базе и не читает .env ----------

def test_order_link_module_has_no_database_access_of_its_own():
    tree = ast.parse((PROJECT_DIR / "accounts" / "order_link.py").read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not imported & {"database", "psycopg", "cors_settings", "dotenv", "commerce.orders", "payments.access"}
    code = (f"import sys; sys.path.insert(0, {str(PROJECT_DIR)!r}); import accounts.order_link; "
            "print(sorted(m for m in ('database', 'cors_settings', 'dotenv') if m in sys.modules))")
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
