"""PR6 C3 (= T-H4), C4 и D4 на настоящем HTTP-стеке и настоящем PostgreSQL (REV5 §6.1, §8; ADDENDUM §4.2).

Стенд — тот же дочерний процесс, что в tests/test_account_disconnect.py (настоящий api.py в uvicorn на 127.0.0.1),
в режиме db: database.get_connection ребёнка открывает НАСТОЯЩЕЕ соединение psycopg (подкласс Connection) к
временной схеме этого теста. Граница транзакции сервиса (AccountService._call): вызов функции -> conn.commit();
выход из `with conn` тоже вызывает commit() (psycopg Connection.__exit__). Затвор стоит в commit() подкласса
ПЕРЕД настоящим COMMIT и только у транзакции с auth_login: ребёнок сообщает AT_COMMIT <pid сервера БД> и ждёт GO.
В момент AT_COMMIT тест сам проверяет, что затвор стоит на настоящей границе: процесс сервера БД — 'idle in
transaction', а результаты входа (сессия, аудит, счётчик) другому соединению ещё не видны.

Различаются три события:
  * отключение HTTP-клиента (C3): сокет закрыт (FIN или RST), uvicorn подтвердил разрыв — затем GO;
  * убийство процесса (C4): stop() завершает опознанный интерпретатор ребёнка, затем процесс Popen (лаунчер venv
    на Windows), пока транзакция открыта, — COMMIT не наступает;
  * штатная остановка (D4): программная (server.should_exit) или сигналом ОС, затем GO.

Безопасность запуска:
  * только явно заданное окружение: PYTHON_DOTENV_DISABLED=1 и TEST_DB_NAME; без них модуль пропускается ДО
    импорта database.py; родитель — под защитой tests/conftest.py (только TEST_DB_NAME) и _lab_role_guard;
  * ребёнок получает окружение с нуля (DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/TEST_DB_NAME — явно) и принимает
    ровно два профиля (решение владельцев), оба на 127.0.0.1:5432, своя схема ds_mig_* роли профиля:
      - лаборатория: digital_store_pr6_stage1_test, роль pr6_stage1_owner без прав суперпользователя;
      - одноразовая база CI (ci/g9_db.py, отдельный обязательный шаг .github/workflows/ci.yml):
        digital_store_g9_ci_test, роль ds_g9_ci_owner без SUPERUSER/CREATEROLE/CREATEDB/REPLICATION/BYPASSRLS,
        владелец базы postgres, маркер базы, USAGE на ext_pgcrypto; адрес — со стороны клиента.
    Окружение проверяется до запуска сервера, а профиль и схема — на каждом настоящем соединении до первого SQL
    магазина; несовпадение — отказ (DB_REFUSED). В других базах тесты модуля падают; основной прогон CI исключает
    модуль (--ignore), отдельный прогон обязателен (ci/check_junit.py --profile g9). psycopg.connect в обход
    фабрики запрещён;
  * всё — во временной схеме make_schema; ролей, прав и общих объектов тест не создаёт и не меняет;
  * у соединений ребёнка application_name = метка теста, lock_timeout 5 с, statement_timeout 30 с,
    idle_in_transaction_session_timeout 60 с; после остановки ребёнка тест ждёт исчезновения его процессов
    сервера БД (не дольше 15 с) — до удаления схемы.
"""

import os

import pytest

# Значения, при которых python-dotenv 1.2 не читает .env (dotenv.main._load_dotenv_disabled).
_DOTENV_DISABLED_VALUES = {"1", "true", "t", "yes", "y"}

# Проверки — до импорта модулей, которые импортируют database.py (он вызывает load_dotenv() при импорте).
if os.environ.get("PYTHON_DOTENV_DISABLED", "").casefold() not in _DOTENV_DISABLED_VALUES:
    pytest.skip("отключение клиента на PostgreSQL: запуск только с PYTHON_DOTENV_DISABLED=1 (без чтения .env)",
                allow_module_level=True)
if "TEST_DB_NAME" not in os.environ:
    pytest.skip("отключение клиента на PostgreSQL: задайте TEST_DB_NAME явно", allow_module_level=True)

import http.client  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402

from test_account_disconnect import (  # noqa: E402,F401  (children — фикстура)
    EVENT_TIMEOUT, PASSWORD, SHOP, _child_env, _drop, _events, _get_status, _login_request, _open, _status,
    children,
)
from test_account_http_db import SCHEMA_RE, _lab_role_guard  # noqa: E402,F401  (_lab_role_guard — фикстура)
from test_migration_0005 import accounts, at_0005, make_schema  # noqa: E402,F401  (фикстуры)
from test_migrations import _query  # noqa: E402

EMAIL = "buyer@example.test"
WRONG_PASSWORD = "Неверный пароль 2026"
BACKEND_GONE_TIMEOUT = 15
_CHILD_DB_ENV = ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "TEST_DB_NAME")


class _LabHandle:
    def __init__(self, owner, start):
        self.owner, self.start = owner, start


@pytest.fixture
def lab(accounts, children, tmp_path):
    """lab.start() -> ребёнок на временной схеме этого теста. Завершение: свои дети убиты, их процессы сервера
    БД исчезли — до удаления схемы (make_schema). Каждый шаг очистки выполняется, даже если предыдущий не
    удался; все ожидания ограничены, неудачи сообщаются ошибкой."""
    owner, schema = accounts
    assert SCHEMA_RE.fullmatch(schema), "работа только во временной схеме make_schema"
    started = []

    def start():
        env = _child_env(tmp_path, {name: os.environ[name] for name in _CHILD_DB_ENV if name in os.environ})
        child = children("db", env, schema)
        started.append(child)
        return child

    yield _LabHandle(owner, start)
    failures = []
    try:
        owner.rollback()                    # тест мог упасть посреди запроса: соединение — в исходное состояние
    except Exception as error:
        failures.append(f"откат соединения владельца: {error}")
    for child in started:
        try:
            child.stop()
        except Exception as error:
            failures.append(f"{child.tag}: остановка: {error}")
        try:
            _wait_backends_gone(owner, "application_name", child.tag)
        except (Exception, pytest.fail.Exception) as error:
            failures.append(f"{child.tag}: процессы сервера БД: {error}")
    assert not failures, f"очистка лаборатории не удалась: {failures}"


def _wait_backends_gone(owner, column, value, timeout=BACKEND_GONE_TIMEOUT):
    """Ограниченное ожидание: процессов сервера БД с этим pid / application_name больше нет."""
    assert column in ("pid", "application_name")
    deadline = time.monotonic() + timeout
    while True:
        rows = _query(owner, f"SELECT pid, state FROM pg_stat_activity WHERE {column} = %s", (value,))
        if not rows:
            return
        if time.monotonic() >= deadline:
            pytest.fail(f"процесс сервера БД не завершился за {timeout} с: {rows}")
        time.sleep(0.05)


def _state(owner):
    """(сессий, записей аудита auth.login, attempts счётчика входа или -1, если строки нет)."""
    return _query(owner, "SELECT (SELECT count(*) FROM customer_sessions), "
                         "(SELECT count(*) FROM audit_log WHERE action = 'auth.login'), "
                         "(SELECT coalesce(max(attempts), -1) FROM auth_throttle WHERE scope = 1)")[0]


REGISTERED = (1, 0, -1)                     # после регистрации: её сессия, входов нет, строки счётчика нет
LOGGED_IN = (2, 1, 0)                       # успешный вход зафиксирован: сессия, аудит, счётчик сброшен
FAILED_ONCE = (1, 0, 1)                     # неверный пароль зафиксирован: попытка учтена


def _register(child):
    """Регистрация через того же ребёнка (auth_register затвором не задерживается)."""
    conn = http.client.HTTPConnection("127.0.0.1", child.port, timeout=EVENT_TIMEOUT)
    try:
        conn.request("POST", "/api/account/register", body=json.dumps({"email": EMAIL, "password": PASSWORD}),
                     headers={"Origin": SHOP, "Content-Type": "application/json"})
        response = conn.getresponse()
        response.read()
    finally:
        conn.close()
    if response.status != 201:                              # например, отказ ребёнка (DB_REFUSED) — в журнале
        raise child._failure(f"регистрация: {response.status} вместо 201")


def _at_commit(owner, child):
    """Ребёнок на затворе: тот же процесс сервера БД держит открытую транзакцию, её результат не виден."""
    event = child.expect("AT_COMMIT")
    assert (event[1], event[3]) == ("db", EMAIL)
    pid = int(event[2])
    assert _query(owner, "SELECT state, application_name FROM pg_stat_activity WHERE pid = %s",
                  (pid,)) == [("idle in transaction", child.tag)]
    assert _state(owner) == REGISTERED                      # до COMMIT ничего не видно
    return pid


def _no_locks_left(owner):
    """Строки покупателя и счётчика никем не заблокированы: короткий lock_timeout, затем откат — и при ошибке
    (иначе соединение владельца осталось бы в прерванной транзакции до очистки)."""
    try:
        with owner.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '200ms'")
            cur.execute("SELECT id FROM customers FOR NO KEY UPDATE")
            cur.fetchall()
            cur.execute("SELECT scope FROM auth_throttle FOR NO KEY UPDATE")
            cur.fetchall()
    finally:
        owner.rollback()


# ---------- C3 (= T-H4): клиент отключился до COMMIT ----------

C3_CASES = {
    "c3a_correct_password": (PASSWORD, LOGGED_IN),
    "c3b_wrong_password": (WRONG_PASSWORD, FAILED_ONCE),
}


@pytest.mark.parametrize("drop", ["fin", "rst"])
@pytest.mark.parametrize("case", C3_CASES)
def test_c3_client_disconnect_before_commit_does_not_prevent_commit(lab, case, drop):
    """Транзакция входа выполнена и ждёт на затворе; клиент закрывает сокет; uvicorn подтверждает разрыв; GO.
    Доказывает: COMMIT происходит после уже обработанного сервером разрыва — успешный вход оставляет сессию,
    запись аудита и сброшенный счётчик (C3a), неверный пароль — учтённую попытку (C3b); блокировок нет, сервер
    жив. НЕ доказывает поведение при разрыве во время самого COMMIT (это C1/C2, test_account_service_db.py)."""
    password, expected = C3_CASES[case]
    child = lab.start()
    _register(child)
    assert _state(lab.owner) == REGISTERED
    sock = _open(child.port, _login_request(child.port, EMAIL, password))
    pid = _at_commit(lab.owner, child)
    _drop(sock, drop)
    child.command("WAIT_DISCONNECTED")
    child.expect("DISCONNECTED")
    child.command("GO")
    assert child.expect("COMMITTED")[1:3] == ["db", str(pid)]
    assert child.expect("ASGI_DONE")[1:] == ["/api/account/login", "response"]
    assert _state(lab.owner) == expected
    _no_locks_left(lab.owner)
    assert _get_status(child.port, "/api/account/me") == 401 and child.alive()


# ---------- C4: процесс приложения убит до COMMIT ----------

def test_c4_child_killed_before_commit_rolls_back_and_releases_locks(lab):
    """Транзакция успешного входа открыта (затвор); тест убивает СВОЙ дочерний процесс. Доказывает: PostgreSQL
    видит разрыв, процесс сервера БД завершается, транзакция откатана — ни сессии, ни аудита, ни строки
    счётчика; блокировки строк покупателя и счётчика сняты. НЕ доказывает убийство посреди SQL-оператора
    (сервер заметил бы разрыв только после оператора: client_connection_check_interval = 0)."""
    child = lab.start()
    _register(child)
    sock = _open(child.port, _login_request(child.port, EMAIL, PASSWORD))
    pid = _at_commit(lab.owner, child)
    code = child.stop()                                     # интерпретатор этого ребёнка, затем процесс Popen
    sock.close()
    assert code != 0
    _wait_backends_gone(lab.owner, "pid", pid)
    assert _state(lab.owner) == REGISTERED
    _no_locks_left(lab.owner)
    assert not _events(child, 0, "COMMITTED")


# ---------- D4: штатная остановка при открытой транзакции ----------

@pytest.mark.parametrize("how", ["programmatic_should_exit", "os_signal"])
def test_d4_graceful_shutdown_with_open_transaction_commits_and_exits(lab, how, record_property):
    """Транзакция успешного входа на затворе; остановка — программная или сигналом ОС (SIGINT на POSIX, SIGBREAK
    на Windows); после «Waiting for connections to close» — GO. Доказывает: при timeout_graceful_shutdown=None
    (умолчание uvicorn) транзакция фиксируется, клиент получает 200, процесс выходит сам, его процессы сервера
    БД исчезают. Код выхода фиксируется (record_property). НЕ доказывает поведение с конечным тайм-аутом
    остановки — боевая команда uvicorn в проекте не описана."""
    child = lab.start()
    _register(child)
    sock = _open(child.port, _login_request(child.port, EMAIL, PASSWORD))
    pid = _at_commit(lab.owner, child)
    signal_name = child.request_shutdown(how)
    child.wait_log("Waiting for connections to close")
    assert _state(lab.owner) == REGISTERED
    child.command("GO")
    assert child.expect("COMMITTED")[1:3] == ["db", str(pid)]
    assert child.expect("ASGI_DONE")[1:] == ["/api/account/login", "response"]
    assert _status(sock) == 200
    code = child.wait_exit()
    record_property("d4_db_stop", signal_name)
    record_property("d4_db_exit_code", code)
    assert _state(lab.owner) == LOGGED_IN
    _wait_backends_gone(lab.owner, "application_name", child.tag)
    _no_locks_left(lab.owner)
    if how == "programmatic_should_exit":
        assert code == 0
