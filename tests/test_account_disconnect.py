"""PR6 Г9 (ADDENDUM §4.2: D1, D3, D4) на настоящем HTTP-стеке, без базы данных.

Что запускается. Дочерний процесс Python (-I -B, окружение собрано с нуля, .env не читается) импортирует
НАСТОЯЩИЙ api.py с включёнными аккаунтами и запускает его в uvicorn (тот же пакет и протокол h11, что в
магазине) на 127.0.0.1, порт выбирает ОС. Подменены только:
  * database.get_connection — поддельное соединение (_FakeConnection): auth_login «возвращает» 'invalid',
    а commit() — затвор: сообщает AT_COMMIT и ждёт команды GO; прямые psycopg.connect запрещены;
  * снаружи api.app — прозрачный наблюдатель ASGI (_observed): только сообщает о начале и конце вызова
    приложения и о том, начат ли ответ; запрос и ответ не меняет.
Отличие от модели ADDENDUM §4.2: маршруты аккаунтов — async def + run_in_threadpool (anyio.to_thread.run_sync,
abandon_on_cancel=False), а не синхронные def из REV5 §6.1; испытывается именно реализация.

Синхронизация — только явные сигналы: строки "DS-G9 ..." в stdout ребёнка и команды в его stdin.
  * PID <pid> <pid родителя> <время создания> — первое событие: кто настоящий интерпретатор (см. _Child);
  * AT_COMMIT  — обработчик дошёл до commit() и стоит на затворе;
  * WAIT_DISCONNECTED -> DISCONNECTED — uvicorn обработал разрыв соединения (server_state.connections пуст);
  * GO -> COMMITTED — затвор открыт, commit() завершён;
  * ASGI / ASGI_DONE <путь> response|no_response / ASGI_CANCELLED — вызов приложения;
  * SHUTDOWN -> server.should_exit = True (программная остановка; это НЕ сигнал ОС);
  * CTRL_BREAK_PRIVATE_CONSOLE -> SIGNAL_SENT — Windows: у ребёнка своя скрытая консоль, и CTRL_BREAK_EVENT
    ей посылает сам интерпретатор, проверив её участников.
Все ожидания ограничены (EVENT_TIMEOUT, EXIT_TIMEOUT), включая очистку; у ребёнка свой сторожевой таймер
(CHILD_WATCHDOG) и затвор с тайм-аутом. Завершаются только процессы этого модуля: процесс Popen и опознанный
интерпретатор, который запустил лаунчер venv (см. _Child).

Что здесь НЕ доказывается: SQL COMMIT (commit() поддельный) — это tests/test_account_disconnect_db.py.
"""

import base64
import http.client
import json
import os
import queue
import secrets
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

if os.name == "nt":
    import _winapi
    import ctypes
    from ctypes import wintypes

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _KERNEL32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    _KERNEL32.GetProcessTimes.restype = wintypes.BOOL

PROJECT_DIR = Path(__file__).resolve().parent.parent
PREFIX = "DS-G9 "
SHOP = "https://shop.example.test"
PASSWORD = "Пароль ёлки 2026"
STARTUP_TIMEOUT = 60
EVENT_TIMEOUT = 30
EXIT_TIMEOUT = 15
CHILD_GATE_TIMEOUT = 30
CHILD_WATCHDOG = 180
D1_REPEATS = 20
_OS_VARS = ("SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC")
_PROCESS_ACCESS = 0x0001 | 0x1000 | 0x00100000     # PROCESS_TERMINATE | QUERY_LIMITED_INFORMATION | SYNCHRONIZE


def _creation_time(handle):
    """Время создания процесса по дескриптору (Windows, FILETIME)."""
    times = [wintypes.FILETIME() for _ in range(4)]
    if not _KERNEL32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime

# Дочерний процесс: настоящий api.py в uvicorn; режим fake — без базы, db — временная схема лаборатории.
_CHILD = r'''
import asyncio, os, queue, re, signal, sys, threading

project, mode, tag = sys.argv[1], sys.argv[2], sys.argv[3]
gate_timeout, watchdog = float(sys.argv[4]), float(sys.argv[5])
schema = sys.argv[6] if mode == "db" else None
sys.path.insert(0, project)

_watchdog = threading.Timer(watchdog, os._exit, args=(3,))     # ребёнок не переживает тест надолго
_watchdog.daemon = True
_watchdog.start()

_out = threading.Lock()


def emit(*parts):
    with _out:
        sys.stdout.write("DS-G9 " + " ".join(str(part) for part in parts) + "\n")
        sys.stdout.flush()


def _own_creation_time():
    """Время создания этого процесса (FILETIME) — вместе с pid однозначно опознаёт процесс; 0, если неизвестно."""
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    k32.GetProcessTimes.restype = wintypes.BOOL
    times = [wintypes.FILETIME() for _ in range(4)]
    if not k32.GetProcessTimes(k32.GetCurrentProcess(), *(ctypes.byref(t) for t in times)):
        return 0
    return times[0].dwHighDateTime << 32 | times[0].dwLowDateTime


# Первое событие: кто на самом деле интерпретатор (в venv на Windows между ним и Popen есть лаунчер).
emit("PID", os.getpid(), os.getppid(), _own_creation_time())


import dotenv, dotenv.main


def _no_dotenv(*args, **kwargs):
    raise AssertionError(".env не должен читаться")


dotenv.find_dotenv = dotenv.main.find_dotenv = _no_dotenv
dotenv.load_dotenv = dotenv.main.load_dotenv = lambda *args, **kwargs: False

import psycopg

_ORIGINAL_CONNECT = psycopg.Connection.__dict__["connect"].__func__


def _forbidden(*args, **kwargs):
    raise AssertionError("подключение к PostgreSQL в обход фабрики теста запрещено")


psycopg.connect = _forbidden
psycopg.Connection.connect = classmethod(_forbidden)
psycopg.AsyncConnection.connect = classmethod(_forbidden)

_gate = queue.Queue()


def wait_gate(kind, pid, email):
    emit("AT_COMMIT", kind, pid, email)
    try:
        go = _gate.get(timeout=gate_timeout)
    except queue.Empty:
        go = None
    if go is None:
        emit("GATE_ABORTED", kind, pid)
        raise RuntimeError("затвор COMMIT: нет команды GO")


class _FakeCursor:
    def __init__(self, conn):
        self.conn, self.row = conn, None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        if "auth_login(" not in str(query):
            raise AssertionError("поддельное соединение: ожидается только auth_login")
        self.conn.login_email = params[0]
        emit("EXECUTE", "fake", params[0])
        self.row = ({"result": "invalid"},)

    def fetchone(self):
        return self.row


class _FakeConnection:
    """Поддельное соединение: auth_login -> 'invalid'; commit() — затвор (SQL нет)."""
    isolation_level = None
    closed = False

    def __init__(self):
        self.login_email = None
        emit("CONNECT", "fake")

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        email, self.login_email = self.login_email, None
        if email is not None:
            wait_gate("fake", 0, email)
            emit("COMMITTED", "fake", 0, email)

    def rollback(self):
        emit("ROLLBACK", "fake")

    def close(self):
        self.closed = True


SCHEMA_RE = re.compile(r"ds_mig_[0-9a-f]{10}")
# Ровно два профиля (решение владельцев): локальная лаборатория и одноразовая база CI (ci/g9_db.py). Других баз,
# ролей и серверов нет; профиль выбирается точным совпадением окружения и подтверждается на каждом соединении.
LAB_DB, LAB_ROLE, LAB_HOST, LAB_PORT = "digital_store_pr6_stage1_test", "pr6_stage1_owner", "127.0.0.1", 5432
CI_DB, CI_ROLE, CI_DB_OWNER = "digital_store_g9_ci_test", "ds_g9_ci_owner", "postgres"
CI_MARKER = "ds-g9-disposable-ci"
PROFILES = {(LAB_DB, LAB_ROLE, LAB_HOST, str(LAB_PORT)): "lab", (CI_DB, CI_ROLE, LAB_HOST, str(LAB_PORT)): "ci"}


def _refuse(reason, detail=""):
    emit("DB_REFUSED", reason)
    raise AssertionError(f"дочерний процесс: {reason} {detail}; разрешены только {LAB_DB} (роль {LAB_ROLE}) и "
                         f"{CI_DB} (роль {CI_ROLE}) на {LAB_HOST}:{LAB_PORT}")


def _check_lab_env():
    """До подключения: окружение указывает ровно на один из двух профилей, схема — временная ds_mig_*."""
    given = tuple(os.environ.get(name) for name in ("TEST_DB_NAME", "DB_USER", "DB_HOST", "DB_PORT"))
    if given not in PROFILES:
        _refuse("env_not_lab_or_ci", given)
    if not SCHEMA_RE.fullmatch(schema or ""):
        _refuse("schema_not_ds_mig", schema)
    return PROFILES[given]


class _GatedCursor(psycopg.Cursor):
    def execute(self, query, params=None, *args, **kwargs):
        if "auth_login(" in str(query):
            self.connection._ds_login_email = params[0]
            emit("EXECUTE", "db", params[0])
        return super().execute(query, params, *args, **kwargs)


class _GatedConnection(psycopg.Connection):
    """Настоящее соединение psycopg; затвор — в commit(), через который идут и явный COMMIT сервиса, и выход
    из `with conn` (Connection.__exit__ вызывает self.commit()). Затвор — только у транзакции с auth_login."""
    _ds_login_email = None
    connect = classmethod(_ORIGINAL_CONNECT)

    def commit(self):
        email, self._ds_login_email = self._ds_login_email, None
        if email is None:
            return super().commit()
        pid = self.info.backend_pid
        if self.info.transaction_status != psycopg.pq.TransactionStatus.INTRANS:
            emit("GATE_NOT_IN_TRANSACTION", pid)
            raise RuntimeError("затвор COMMIT: транзакция не открыта")
        wait_gate("db", pid, email)
        super().commit()
        emit("COMMITTED", "db", pid, email)


def _db_connection():
    """Настоящее соединение для теста. До первого SQL магазина на нём же проверяется профиль, затем — что
    search_path ведёт ровно в свою схему ds_mig_*, созданную ролью профиля. Любое несовпадение — отказ,
    соединение закрывается.
      * lab: база, роль (и что она не суперпользователь), адрес и порт сервера (127.0.0.1:5432 с его стороны);
      * ci: база, роль и её атрибуты (без SUPERUSER/CREATEROLE/CREATEDB/REPLICATION/BYPASSRLS), владелец и
        маркер базы, USAGE на ext_pgcrypto; адрес — со стороны клиента (conn.info: 127.0.0.1:5432), со стороны
        сервера — TCP и порт 5432 (сервис CI — контейнер с проброшенным портом, свой адрес он видит не loopback)."""
    profile = _check_lab_env()
    dbname, role = (LAB_DB, LAB_ROLE) if profile == "lab" else (CI_DB, CI_ROLE)
    conn = _GatedConnection.connect(host=LAB_HOST, port=LAB_PORT, dbname=dbname, user=role,
                                    password=os.environ.get("DB_PASSWORD"), application_name=tag,
                                    connect_timeout=10, cursor_factory=_GatedCursor)
    try:
        with conn.cursor() as cur:
            if profile == "lab":
                cur.execute("SELECT current_database(), current_user, session_user, r.rolsuper, "
                            "host(inet_server_addr()), inet_server_port() "
                            "FROM pg_roles r WHERE r.rolname = current_user")
                identity = cur.fetchall()
                if identity != [(LAB_DB, LAB_ROLE, LAB_ROLE, False, LAB_HOST, LAB_PORT)]:
                    _refuse("identity_not_lab", identity)
            else:
                cur.execute("SELECT current_database(), current_user, session_user, r.rolsuper, r.rolcreaterole, "
                            "r.rolcreatedb, r.rolreplication, r.rolbypassrls, inet_server_addr() IS NOT NULL, "
                            "inet_server_port(), shobj_description(d.oid, 'pg_database'), "
                            "pg_get_userbyid(d.datdba), has_schema_privilege('ext_pgcrypto', 'USAGE') "
                            "FROM pg_roles r, pg_database d "
                            "WHERE r.rolname = current_user AND d.datname = current_database()")
                identity = (conn.info.hostaddr, conn.info.port, cur.fetchall())
                expected = [(CI_DB, CI_ROLE, CI_ROLE, False, False, False, False, False, True, LAB_PORT, CI_MARKER,
                             CI_DB_OWNER, True)]
                if identity != (LAB_HOST, LAB_PORT, expected):
                    _refuse("identity_not_ci", identity)
            for statement in ("SET lock_timeout = '5s'", "SET statement_timeout = '30s'",
                              "SET idle_in_transaction_session_timeout = '60s'", f"SET search_path TO {schema}"):
                cur.execute(statement)
            cur.execute("SELECT current_schemas(false), pg_get_userbyid(n.nspowner) FROM pg_namespace n "
                        "WHERE n.nspname = %s", (schema,))
            placement = cur.fetchall()
            if placement != [([schema], role)]:
                _refuse("schema_not_own", placement)
        conn.commit()
    except BaseException:
        conn.close()
        raise
    emit("CONNECT", "db", conn.info.backend_pid)
    return conn


if mode == "db":
    _check_lab_env()                                        # отказ до запуска сервера: DB_REFUSED вместо PORT

import database

database.get_connection = _db_connection if mode == "db" else _FakeConnection

import api
import uvicorn


async def _observed(scope, receive, send):
    """Прозрачный наблюдатель вокруг настоящего api.app: запрос и ответ не меняет."""
    if scope["type"] != "http":
        return await api.app(scope, receive, send)
    path, started = scope["path"], False

    async def tracking_send(message):
        nonlocal started
        if message["type"] == "http.response.start":
            started = True
        await send(message)

    emit("ASGI", path)
    try:
        await api.app(scope, receive, tracking_send)
    except asyncio.CancelledError:
        emit("ASGI_CANCELLED", path)
        raise
    emit("ASGI_DONE", path, "response" if started else "no_response")


LOOP = None


class _Server(uvicorn.Server):
    async def startup(self, sockets=None):
        global LOOP
        await super().startup(sockets=sockets)
        LOOP = asyncio.get_running_loop()
        emit("PORT", self.servers[0].sockets[0].getsockname()[1])


server = _Server(uvicorn.Config(_observed, host="127.0.0.1", port=0, log_level="info", access_log=False))


def _on_loop(predicate, timeout=10.0):
    async def poll():
        deadline = LOOP.time() + timeout
        while not predicate():
            if LOOP.time() >= deadline:
                return False
            await asyncio.sleep(0.01)
        return True
    return asyncio.run_coroutine_threadsafe(poll(), LOOP).result(timeout + 5)


def _ctrl_break_private_console(args):
    """CTRL_BREAK_EVENT всем процессам СВОЕЙ скрытой консоли: GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, 0).
    Консоль создана для этого ребёнка (CREATE_NEW_CONSOLE; признак DS_G9_PRIVATE_CONSOLE родитель ставит вместе
    с флагом), консоль pytest и PowerShell — другая. Перед вызовом GetConsoleProcessList должен вернуть ровно
    этот интерпретатор и, если Popen запустил лаунчер, его — прямого родителя с pid из Popen. Любое другое
    содержимое, ошибка API или лишние участники — отказ без сигнала; ошибка вызова — провал, без другой отправки.
    args: pid процесса Popen (лаунчер или сам интерпретатор) и pid pytest."""
    if os.name != "nt" or os.environ.get("DS_G9_PRIVATE_CONSOLE") != "1" or len(args) != 2:
        emit("SIGNAL_REFUSED", "not_private_console")
        return
    try:
        popen_pid, pytest_pid = int(args[0]), int(args[1])
    except ValueError:
        emit("SIGNAL_REFUSED", "bad_arguments")
        return
    me = os.getpid()
    if popen_pid == me:
        expected = [me]
    elif popen_pid == os.getppid():
        expected = sorted((me, popen_pid))
    else:
        emit("SIGNAL_REFUSED", "unexpected_parent", popen_pid, os.getppid())
        return
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetConsoleProcessList.argtypes = (ctypes.POINTER(wintypes.DWORD), wintypes.DWORD)
    k32.GetConsoleProcessList.restype = wintypes.DWORD
    k32.GenerateConsoleCtrlEvent.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k32.GenerateConsoleCtrlEvent.restype = wintypes.BOOL
    buffer = (wintypes.DWORD * 8)()
    count = k32.GetConsoleProcessList(buffer, len(buffer))
    members = sorted(buffer[:count]) if 0 < count <= len(buffer) else None
    if members != expected or pytest_pid in members:
        emit("SIGNAL_REFUSED", "console_members", ",".join(map(str, members or [])) or "none", count)
        return
    if not k32.GenerateConsoleCtrlEvent(signal.CTRL_BREAK_EVENT, 0):
        emit("SIGNAL_FAILED", ctypes.get_last_error())
        return
    emit("SIGNAL_SENT", "SIGBREAK", ",".join(map(str, members)))


def _commands():
    for line in sys.stdin:
        command, *args = line.split() or [""]
        if command == "GO":
            _gate.put(True)
        elif command == "WAIT_DISCONNECTED":
            emit("DISCONNECTED" if _on_loop(lambda: not server.server_state.connections) else "WAIT_TIMEOUT")
        elif command == "SHUTDOWN":
            LOOP.call_soon_threadsafe(setattr, server, "should_exit", True)
            emit("SHUTDOWN_REQUESTED")
        elif command == "CTRL_BREAK_PRIVATE_CONSOLE":
            _ctrl_break_private_console(args)
    _gate.put(None)                                         # stdin закрыт: затвор не ждёт, сервер выходит
    try:
        LOOP.call_soon_threadsafe(setattr, server, "should_exit", True)
    except (AttributeError, RuntimeError):
        pass


threading.Thread(target=_commands, daemon=True).start()
server.run()
emit("SERVE_RETURNED")
'''


class _Child:
    """Дочерний uvicorn с настоящим api.py. Дерево процессов ребёнка:
      * POSIX и Python без лаунчера: Popen запускает сам интерпретатор — один процесс;
      * venv на Windows: sys.executable — лаунчер Python (py.exe), Popen запускает лаунчер, а тот — настоящий
        интерпретатор (внук теста) в той же консоли.
    Интерпретатор первым событием сообщает PID <pid> <pid родителя> <время создания>. Если это не процесс Popen,
    тест открывает его дескриптор и опознаёт: родитель — наш лаунчер, время создания совпадает (pid и время
    создания однозначно задают процесс, а открытый дескриптор не даёт pid перейти к другому). stop() завершает
    ровно эти процессы и проверяет, что завершились оба; завершение лаунчера само по себе не считается
    доказательством. Процессы, которые не удалось опознать, тест не завершает — это ошибка теста.
    uvicorn запускается без --reload и без workers: других процессов дерево не порождает."""

    def __init__(self, tmp_path, mode, env, schema=None):
        self.tag = f"ds-g9-{uuid.uuid4().hex[:12]}"
        argv = [sys.executable, "-I", "-B", "-X", "utf8", "-c", _CHILD, str(PROJECT_DIR), mode, self.tag,
                str(CHILD_GATE_TIMEOUT), str(CHILD_WATCHDOG)] + ([schema] if schema else [])
        # Windows: своя скрытая консоль (CREATE_NEW_CONSOLE + SW_HIDE; CREATE_NEW_PROCESS_GROUP при ней
        # игнорируется) — Ctrl+C и CTRL_BREAK консоли pytest до ребёнка не доходят, а его CTRL_BREAK — до pytest
        # и PowerShell. Признак DS_G9_PRIVATE_CONSOLE ставится только вместе с флагом. POSIX: свой сеанс.
        if os.name == "nt":
            hidden = subprocess.STARTUPINFO(dwFlags=subprocess.STARTF_USESHOWWINDOW,
                                            wShowWindow=subprocess.SW_HIDE)
            isolation = {"creationflags": subprocess.CREATE_NEW_CONSOLE, "startupinfo": hidden}
            env = {**env, "DS_G9_PRIVATE_CONSOLE": "1"}
        else:
            isolation = {"start_new_session": True}
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
                                     cwd=tmp_path, **isolation)
        self.events, self.log_lines, self.seen, self.log = queue.Queue(), queue.Queue(), [], []
        self.pumps = []
        for stream, protocol in ((self.proc.stdout, True), (self.proc.stderr, False)):
            pump = threading.Thread(target=self._pump, args=(stream, protocol), daemon=True)
            pump.start()
            self.pumps.append((pump, stream))
        self.port = None
        # Дерево: interpreter_pid None — ещё не опознано; == proc.pid — интерпретатор и есть процесс Popen;
        # иначе _interpreter — дескриптор опознанного внука (после доказанного завершения закрывается).
        self.interpreter_pid, self._interpreter, self._interpreter_closed = None, None, False

    def _pump(self, stream, protocol):
        for line in stream:
            line = line.rstrip("\n")
            if protocol and line.startswith(PREFIX):
                self.events.put(line[len(PREFIX):].split(" "))
            else:
                self.log.append(line)
                self.log_lines.put(line)
        (self.events if protocol else self.log_lines).put(None)

    def _failure(self, what):
        return AssertionError(f"{what}; события: {self.seen[-12:]}; журнал ребёнка: {self.log[-25:]}")

    def wait_port(self):
        self._adopt_interpreter(self.expect("PID", STARTUP_TIMEOUT))
        self.port = int(self.expect("PORT", STARTUP_TIMEOUT)[1])
        return self

    def _adopt_interpreter(self, event):
        """Опознать настоящий интерпретатор. Сам процесс Popen — ничего не нужно. Иначе только Windows и только
        прямой потомок нашего лаунчера (его pid занят, пока жив объект Popen) с тем же временем создания;
        иначе — ошибка, и неопознанный процесс тест не трогает."""
        pid, parent, created = (int(value) for value in event[1:4])
        if pid == self.proc.pid:
            self.interpreter_pid = pid
            return
        if os.name != "nt" or parent != self.proc.pid or created == 0:
            raise self._failure(f"неожиданное дерево: Popen {self.proc.pid}, интерпретатор {pid}, "
                                f"родитель {parent}")
        try:
            handle = _winapi.OpenProcess(_PROCESS_ACCESS, False, pid)
        except OSError as error:
            raise self._failure(f"интерпретатор {pid} недоступен: {error!r}") from None
        try:
            same = _creation_time(handle) == created
        except OSError:
            same = False
        if not same:
            _winapi.CloseHandle(handle)
            raise self._failure(f"процесс {pid} не опознан как интерпретатор этого ребёнка")
        self.interpreter_pid, self._interpreter = pid, handle

    def _interpreter_exited(self, timeout):
        """Доказано, что интерпретатор завершён (ожидание не дольше timeout с): сам процесс Popen — по Popen,
        опознанный внук — по его дескриптору. Неопознанный интерпретатор завершённым не считается."""
        if self._interpreter_closed:
            return True                                     # завершение уже доказано в stop()
        if self.interpreter_pid is None:
            return False
        if self._interpreter is None:                       # интерпретатор — сам процесс Popen
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                return False
            return True
        return _winapi.WaitForSingleObject(self._interpreter, int(timeout * 1000)) == _winapi.WAIT_OBJECT_0

    def expect(self, name, timeout=EVENT_TIMEOUT):
        """Следующее событие name (предыдущие — в self.seen). Сбой затвора или ожидания — сразу ошибка."""
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                event = self.events.get(timeout=remaining)
            except queue.Empty:
                break
            if event is None:
                raise self._failure(f"ребёнок закрыл stdout, не дождавшись {name}")
            self.seen.append(event)
            if event[0] == name:
                return event
            if event[0] in ("GATE_ABORTED", "GATE_NOT_IN_TRANSACTION", "WAIT_TIMEOUT", "ASGI_CANCELLED",
                            "SIGNAL_REFUSED", "SIGNAL_FAILED", "DB_REFUSED"):
                raise self._failure(f"{event[0]} вместо {name}")
        raise self._failure(f"нет события {name} за {timeout} с")

    def wait_log(self, fragment, timeout=EVENT_TIMEOUT):
        deadline = time.monotonic() + timeout
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                line = self.log_lines.get(timeout=remaining)
            except queue.Empty:
                break
            if line is None:
                break
            if fragment in line:
                return line
        raise self._failure(f"нет строки журнала {fragment!r} за {timeout} с")

    def command(self, text):
        """Команда в stdin ребёнка. Если он уже завершился, канал закрыт — ошибка, команда не выполнена."""
        try:
            self.proc.stdin.write(text + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as error:
            raise self._failure(f"команда {text} не доставлена: {error!r}") from None

    def alive(self):
        """Ребёнок работает: жив процесс Popen, а если интерпретатор — отдельный опознанный внук, то и он."""
        if self.proc.poll() is not None:
            return False
        return self._interpreter is None or not self._interpreter_exited(0)

    def any_alive(self):
        """Жив хотя бы один известный процесс ребёнка: процесс Popen или опознанный внук."""
        if self.proc.poll() is None:
            return True
        return self._interpreter is not None and not self._interpreter_exited(0)

    def tree_unproven(self):
        """Интерпретатор не опознан (нет события PID или опознание не прошло): его завершение не доказать."""
        return self.interpreter_pid is None

    def request_shutdown(self, how):
        """Штатная остановка: программная (server.should_exit) или настоящим сигналом ОС, который uvicorn
        обрабатывает (HANDLED_SIGNALS): SIGINT на POSIX, SIGBREAK (CTRL_BREAK_EVENT) на Windows.

        Windows: CTRL_BREAK_EVENT из pytest не посылается. Его посылает сам интерпретатор ребёнка своей скрытой
        консоли (GenerateConsoleCtrlEvent с группой 0), проверив GetConsoleProcessList: в консоли только он и
        его лаунчер (см. _ctrl_break_private_console). Консоль pytest и PowerShell другая, поэтому событие до них
        не доходит; если ребёнок уже завершился, команда не доставляется и сигнал не посылается никому.
        POSIX: SIGINT только pid ребёнка (лаунчера на POSIX нет: Popen — сам интерпретатор); Popen.send_signal
        сам вызывает poll(), а pid завершившегося ребёнка не может достаться другому процессу до wait()."""
        if not self.alive():
            raise self._failure(f"ребёнок уже завершился (код {self.proc.poll()}), остановка не запрошена")
        if how == "programmatic_should_exit":
            self.command("SHUTDOWN")
            self.expect("SHUTDOWN_REQUESTED")
            return "should_exit"
        if os.name == "nt":
            self.command(f"CTRL_BREAK_PRIVATE_CONSOLE {self.proc.pid} {os.getpid()}")
            self.expect("SIGNAL_SENT")
            return "SIGBREAK"
        if self.interpreter_pid != self.proc.pid:
            raise self._failure("POSIX: интерпретатор — не процесс Popen, SIGINT не посылается")
        self.proc.send_signal(signal.SIGINT)
        return "SIGINT"

    def wait_exit(self, timeout=EXIT_TIMEOUT):
        """Ребёнок выходит сам: процесс Popen и опознанный интерпретатор (каждое ожидание — не дольше timeout)."""
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            raise self._failure(f"процесс не завершился сам за {timeout} с") from None
        if not self._interpreter_exited(timeout):
            raise self._failure(f"интерпретатор {self.interpreter_pid} не завершился за {timeout} с")
        return code

    def stop(self):
        """Идемпотентно: завершить ровно своё дерево и закрыть каналы. Сначала опознанный интерпретатор
        (TerminateProcess по его дескриптору — так C4 остаётся убийством процесса с открытой транзакцией),
        затем процесс Popen; завершение каждого проверяется отдельно. Все ожидания ограничены; stdout/stderr
        закрываются только после того, как их читатели дошли до конца: иначе close() ждал бы блокировку буфера,
        которую держит читатель. Процесс пережил завершение или канал не закрылся — ошибка, а не зависание.
        Неопознанный интерпретатор тест не трогает и сообщает ошибкой, что его завершение не доказано."""
        if self._interpreter is not None and not self._interpreter_exited(0):
            try:
                _winapi.TerminateProcess(self._interpreter, 1)
            except OSError:
                pass                                        # итог проверяется ниже: не завершился — ошибка
        if self.proc.poll() is None:
            self.proc.kill()
        try:
            self.proc.wait(timeout=EXIT_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise self._failure(f"процесс {self.proc.pid} не завершился за {EXIT_TIMEOUT} с после kill()") from None
        finally:
            try:
                self.proc.stdin.close()                     # пишет только тест, буфер пуст (flush после команды)
            except (OSError, ValueError):
                pass                                        # ребёнок уже не читает: своя сторона канала закрыта
        if self.tree_unproven():
            raise self._failure("интерпретатор не опознан: его завершение не доказано, тест его не трогает")
        if not self._interpreter_exited(EXIT_TIMEOUT):
            raise self._failure(f"интерпретатор {self.interpreter_pid} не завершился за {EXIT_TIMEOUT} с")
        if self._interpreter is not None:
            _winapi.CloseHandle(self._interpreter)          # завершён: дескриптор больше не нужен
            self._interpreter, self._interpreter_closed = None, True
        for pump, stream in self.pumps:
            pump.join(EXIT_TIMEOUT)
            if pump.is_alive():
                raise self._failure(f"канал вывода процесса {self.proc.pid} не закрылся за {EXIT_TIMEOUT} с")
            stream.close()
        return self.proc.returncode


@pytest.fixture
def children(tmp_path):
    """children(mode, env, schema=None) -> _Child. В конце теста все свои процессы завершены; очистка каждого
    выполняется, даже если очистка предыдущего не удалась, а неудачи сообщаются ошибкой."""
    started = []

    def start(mode, env, schema=None):
        child = _Child(tmp_path, mode, env, schema)
        started.append(child)
        return child.wait_port()

    yield start
    failures = []
    for child in started:
        try:
            child.stop()
        except Exception as error:
            failures.append(f"{child.tag}: {error}")
    alive = [child.tag for child in started if child.any_alive()]
    unproven = [child.tag for child in started if child.tree_unproven()]
    assert not failures and not alive and not unproven, (f"очистка дочерних процессов не удалась: {failures}; "
                                                         f"живые: {alive}; не опознаны: {unproven}")


def _secrets_file(tmp_path):
    """Тестовый файл секретов аккаунтов (случайные значения, не настоящие секреты)."""
    def b64():
        return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    path = tmp_path / f"account-secrets-{uuid.uuid4().hex[:8]}.env"
    path.write_text(f'ACCOUNT_PEPPER_CURRENT="1:{b64()}"\nACCOUNT_CSRF_KEY="{b64()}"\n', encoding="utf-8")
    return path


def _child_env(tmp_path, extra=None):
    """Окружение ребёнка с нуля: системные переменные ОС и явные настройки; .env, PG*, DB_* родителя не наследуются.
    HTTP_RATE_LIMITS=off — иначе 20 входов с одного адреса упрутся в общий лимит раньше обработчика."""
    env = {name: os.environ[name] for name in _OS_VARS if name in os.environ}
    env.update({"PYTHON_DOTENV_DISABLED": "1", "DS_ACCOUNTS_ENABLED": "on",
                "DS_ACCOUNT_SECRETS_FILE": str(_secrets_file(tmp_path)), "ACCOUNT_TRUSTED_ORIGINS": SHOP,
                "CORS_ALLOWED_ORIGINS": SHOP, "HTTP_RATE_LIMITS": "off", "PAYMENTS_ENABLED": "false"})
    env.update(extra or {})
    return env


def _login_request(port, email, password):
    body = json.dumps({"email": email, "password": password}).encode("ascii")
    head = (f"POST /api/account/login HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: {SHOP}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n")
    return head.encode("ascii") + body


def _open(port, payload):
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.sendall(payload)
    return sock


def _drop(sock, how):
    """fin — обычное закрытие; rst — обрыв (SO_LINGER 0)."""
    if how == "rst":
        linger = struct.pack("HH", 1, 0) if os.name == "nt" else struct.pack("ii", 1, 0)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
    sock.close()


def _status(sock, timeout=EXIT_TIMEOUT):
    """Код ответа из строки статуса; соединение закрывается."""
    sock.settimeout(timeout)
    data = b""
    try:
        while b"\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    finally:
        sock.close()
    line = data.split(b"\r\n", 1)[0].decode("latin-1")
    assert line.startswith("HTTP/1.1 "), f"нет строки статуса: {line!r}"
    return int(line.split(" ")[1])


def _get_status(port, path, timeout=10):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def _events(child, start, *names):
    return [event for event in child.seen[start:] if event[0] in names]


# ---------- D1: клиент отключился, обработчик доведён до конца ----------

def test_d1_disconnected_client_does_not_stop_the_handler(children, tmp_path):
    """D1 ×20: полный запрос входа; обработчик (в пуле потоков) стоит на затворе commit(); клиент закрывает
    сокет (чередуя FIN и RST); uvicorn подтверждает разрыв (connections пуст) — и только потом GO.
    Доказывает: разрыв, уже обработанный сервером, не отменяет обработчик — commit() вызван и завершён,
    приложение вернуло ответ (ASGI_DONE response), отмены задачи нет. НЕ доказывает SQL COMMIT."""
    child = children("fake", _child_env(tmp_path))
    for number in range(D1_REPEATS):
        email = f"d1-{number}@example.test"
        start = len(child.seen)
        sock = _open(child.port, _login_request(child.port, email, PASSWORD))
        assert child.expect("AT_COMMIT")[1:] == ["fake", "0", email]
        _drop(sock, "rst" if number % 2 else "fin")
        child.command("WAIT_DISCONNECTED")
        child.expect("DISCONNECTED")
        child.command("GO")
        assert child.expect("COMMITTED")[1:] == ["fake", "0", email]
        assert child.expect("ASGI_DONE")[1:] == ["/api/account/login", "response"]
        assert not _events(child, start, "ROLLBACK", "ASGI_CANCELLED")
    assert len(_events(child, 0, "COMMITTED")) == D1_REPEATS
    assert _get_status(child.port, "/api/account/me") == 401 and child.alive()


# ---------- D3: неполное тело — обработчик не запускается ----------

def test_d3_incomplete_body_never_reaches_the_handler(children, tmp_path):
    """D3: заголовки и половина тела, затем закрытие. Приложение вызвано (ASGI), но вернулось без ответа
    (ASGI_DONE no_response): RequestLimitsMiddleware получил http.disconnect, маршрут и сервис не вызывались
    — нет CONNECT/EXECUTE/AT_COMMIT между началом и концом вызова. Затем тот же сервер с той же разметкой
    видит полный запрос — отсутствие событий не следствие неработающего наблюдения."""
    child = children("fake", _child_env(tmp_path))
    start = len(child.seen)
    payload = _login_request(child.port, "d3@example.test", PASSWORD)
    sock = _open(child.port, payload[:len(payload) - 20])
    _drop(sock, "fin")
    assert child.expect("ASGI")[1:] == ["/api/account/login"]
    assert child.expect("ASGI_DONE")[1:] == ["/api/account/login", "no_response"]
    assert not _events(child, start, "CONNECT", "EXECUTE", "AT_COMMIT", "COMMITTED")
    control = _open(child.port, _login_request(child.port, "control@example.test", PASSWORD))
    assert child.expect("AT_COMMIT")[1:] == ["fake", "0", "control@example.test"]
    child.command("GO")
    child.expect("COMMITTED")
    assert _status(control) == 401                          # поддельный auth_login: 'invalid'
    assert child.alive()


# ---------- D4: штатная остановка во время запроса ----------

@pytest.mark.parametrize("how", ["programmatic_should_exit", "os_signal"])
def test_d4_graceful_shutdown_waits_for_the_handler(children, tmp_path, how, record_property):
    """D4: обработчик на затворе; остановка — программная (server.should_exit) или сигналом ОС (SIGINT на POSIX,
    SIGBREAK на Windows — их обрабатывает uvicorn). Только после строки uvicorn «Waiting for connections to
    close» (остановка началась и ждёт этот запрос) — GO. Доказывает: при timeout_graceful_shutdown=None
    (умолчание uvicorn) обработчик доходит до commit(), клиент получает ответ, процесс выходит сам.
    Код выхода фиксируется (record_property): после сигнала uvicorn повторно поднимает его — код зависит от ОС.
    НЕ доказывает: поведение с конечным --timeout-graceful-shutdown и SQL COMMIT."""
    child = children("fake", _child_env(tmp_path))
    sock = _open(child.port, _login_request(child.port, "d4@example.test", PASSWORD))
    child.expect("AT_COMMIT")
    signal_name = child.request_shutdown(how)
    child.wait_log("Waiting for connections to close")
    child.command("GO")
    child.expect("COMMITTED")
    assert child.expect("ASGI_DONE")[1:] == ["/api/account/login", "response"]
    assert _status(sock) == 401
    code = child.wait_exit()
    record_property("d4_stop", signal_name)
    record_property("d4_exit_code", code)
    if how == "programmatic_should_exit":
        assert code == 0 and child.expect("SERVE_RETURNED")
