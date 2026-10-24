"""Сервис аккаунтов покупателей: вызовы функций базы миграции 0005 (PR6_DESIGN_REV4 §3, REV5 §5, §6,
ADDENDUM2 §1.2). HTTP-маршрутов здесь нет (этап 2d), привязки заказа — тоже (этап 2e).

Правила:
  * одна операция — одно соединение и одна транзакция: вызов ОДНОЙ функции, затем явный COMMIT;
    уровень изоляции READ COMMITTED (функции базы отказывают при другом);
  * в базу уходят только HMAC-доказательства пароля (accounts.proof) и SHA-256 токенов сессий
    (accounts.session); пароль и сам токен в SQL не попадают;
  * отказы с учётом попытки (invalid, throttled, …) — значения результата: транзакция фиксируется, иначе
    пропал бы учёт; результат отдаётся вызывающему только после подтверждённого COMMIT (REV5 §6.2);
  * COMMIT не подтверждён (обрыв соединения, ошибка при фиксации) — исход неизвестен. Если операция
    создавала сессию (регистрация, вход, смена пароля), выполняется ОДИН вызов auth_session_resolve на новом
    соединении: сессия действительна — операция зафиксирована, иначе ошибка «повторите». Сама операция
    повторно не выполняется. Для остальных операций — сразу «повторите» (I2: 503 при любом результате);
  * ошибка базы до COMMIT (statement_timeout, отмена, обрыв, конфликт) — ROLLBACK и «повторите»;
    ошибка контракта (RAISE функции при данных, уже проверенных здесь; неожиданный ответ) — «внутренняя»;
  * регистрация, вход и смена пароля выполняют bcrypt в базе: одновременно — не больше bcrypt_slots на
    процесс (неблокирующий семафор, по умолчанию 2, REV5 §4.1). Нет свободного места — отказ сразу, без
    обращения к базе; место освобождается в finally при любом исходе;
  * при выключенных аккаунтах (DS_ACCOUNTS_ENABLED) ни один метод к базе не обращается.

Ошибки (AccountServiceError, AccountInputError, PasswordPolicyError) не содержат SQL, параметров, паролей,
доказательств, токенов и текста ошибок базы; в AccountServiceError.diagnostic — только SQLSTATE и класс.
Cookie выставляет маршрут, и только по SessionOutcome.session (есть лишь после подтверждённой фиксации).
"""

import re
import threading
from dataclasses import dataclass

import psycopg

from accounts.config import AccountSettings
from accounts.proof import password_proofs
from accounts.session import SessionToken

# Контракт email_norm — как в базе (auth_check_email, auth_check_not_reserved). Нормализация ввода
# покупателя (регистр, пробелы, Unicode) — не здесь: правило ещё не утверждено (открытый вопрос PR6).
# Шаблон базы '^[^@\s]+@[^@\s]+$' применяется ко всей строке (fullmatch): у re.match '$' допускает '\n'
# в конце, у PostgreSQL — нет.
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+")
# Не передаётся в PostgreSQL как text: U+0000 (DataError psycopg) и одиночные суррогаты (не кодируются в UTF-8).
_NOT_STORABLE_RE = re.compile(r"[\x00\ud800-\udfff]")
MAX_EMAIL_CHARS = 254
DEFAULT_BCRYPT_SLOTS = 2

SQL_REGISTER = "SELECT auth_register(%s, %s, %s::smallint, %s)"
SQL_LOGIN = "SELECT auth_login(%s, %s, %s::smallint, %s, %s::smallint, %s, %s)"
SQL_PASSWORD_CHANGE = "SELECT auth_password_change(%s, %s, %s::smallint, %s, %s::smallint, %s, %s::smallint, %s)"
SQL_RESOLVE = "SELECT auth_session_resolve(%s)"
SQL_TOUCH = "SELECT auth_session_touch(%s)"
SQL_REVOKE = "SELECT auth_session_revoke(%s)"
SQL_REVOKE_ALL = "SELECT auth_sessions_revoke_all(%s)"
SQL_PROFILE = "SELECT customer_profile_view(%s)"
SQL_ORDERS = "SELECT customer_orders_view(%s)"
SQL_ORDER = "SELECT customer_order_view(%s, %s)"

# Допустимые результаты функций 0005 (всё остальное — нарушение контракта).
REGISTER_RESULTS = frozenset({"created", "email_taken", "busy", "disabled", "closed"})
LOGIN_RESULTS = frozenset({"ok", "invalid", "throttled", "busy", "disabled"})
PASSWORD_CHANGE_RESULTS = frozenset({"ok", "invalid", "session_invalid", "throttled", "busy", "disabled"})
REVOKE_ALL_RESULTS = frozenset({"ok", "session_invalid", "busy", "disabled"})


class AccountServiceError(Exception):
    """Операция не выполнена. code: retry (503) | overloaded (503) | internal (500) | disabled (404)."""

    STATUS = {"retry": 503, "overloaded": 503, "internal": 500, "disabled": 404}

    def __init__(self, code, diagnostic=None):
        super().__init__(code)
        self.code = code
        self.http_status = self.STATUS[code]
        self.diagnostic = diagnostic          # только SQLSTATE и имя класса ошибки — для журнала сервера


class AccountInputError(ValueError):
    """Неверные входные данные (HTTP 422). Значение в сообщение не включается."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SessionOutcome:
    """Итог регистрации, входа или смены пароля. session — только при успехе ПОСЛЕ подтверждённого COMMIT
    (или подтверждённого resolve при неизвестном исходе COMMIT); только тогда маршрут выставляет cookie."""

    result: str
    session: SessionToken = None
    retry_after_seconds: int = None


def check_email_norm(email_norm):
    """Тот же контракт, что auth_check_email и auth_check_not_reserved в базе, и только строки, которые можно
    передать в базу: иначе ввод покупателя дал бы 500/503 вместо 422."""
    if (not isinstance(email_norm, str) or len(email_norm) > MAX_EMAIL_CHARS or email_norm != email_norm.lower()
            or not _EMAIL_RE.fullmatch(email_norm) or _NOT_STORABLE_RE.search(email_norm)):
        raise AccountInputError("email_invalid")
    if email_norm.endswith("@invalid") or email_norm.endswith(".invalid"):
        raise AccountInputError("email_reserved")
    return email_norm


def _check_session(session):
    if not isinstance(session, SessionToken):
        raise TypeError("session: ожидается SessionToken")


def _diagnostic(error):
    return f"{type(error).__name__}:{getattr(error, 'sqlstate', None) or '-'}"


def _default_connect():
    # Импорт здесь: database.py при импорте читает .env (load_dotenv) — только в работающем приложении.
    from database import get_connection
    return get_connection()


class AccountService:
    def __init__(self, settings, connect=None, bcrypt_slots=DEFAULT_BCRYPT_SLOTS):
        if not isinstance(settings, AccountSettings):
            raise TypeError("settings: ожидается AccountSettings")
        if isinstance(bcrypt_slots, bool) or not isinstance(bcrypt_slots, int) or bcrypt_slots < 1:
            raise ValueError("bcrypt_slots: целое число не меньше 1")
        self._settings = settings
        self._connect = connect or _default_connect
        self._bcrypt = threading.BoundedSemaphore(bcrypt_slots)

    def __repr__(self):
        return f"AccountService(enabled={self._settings.enabled})"

    # ---------- транзакции ----------

    def _require_enabled(self):
        if not self._settings.enabled:
            raise AccountServiceError("disabled")

    def _open(self):
        try:
            conn = self._connect()
            conn.isolation_level = psycopg.IsolationLevel.READ_COMMITTED
        except psycopg.Error as error:
            raise AccountServiceError("retry", _diagnostic(error)) from None
        return conn

    def _call(self, sql, params, validate):
        """Вызов одной функции, проверка ответа (validate) ДО фиксации и COMMIT.

        -> (значение, committed: bool). committed=False — исход COMMIT неизвестен. Ответ вне контракта —
        ROLLBACK и «внутренняя ошибка» (ничего не фиксируется)."""
        conn = self._open()
        try:
            try:
                with conn.cursor() as cursor:
                    cursor.execute(sql, params)
                    row = cursor.fetchone()
            except psycopg.errors.RaiseException as error:
                _quiet_rollback(conn)
                raise AccountServiceError("internal", _diagnostic(error)) from None
            except psycopg.Error as error:
                _quiet_rollback(conn)
                raise AccountServiceError("retry", _diagnostic(error)) from None
            value = row[0] if row else None
            if not validate(value):
                _quiet_rollback(conn)
                raise AccountServiceError("internal", "unexpected_result")
            try:
                conn.commit()
            except psycopg.Error:
                return value, False
            return value, True
        finally:
            _quiet_close(conn)

    def _session_valid_after_unknown_commit(self, session):
        """Один вызов auth_session_resolve на новом соединении (REV5 §6.2). Ошибка — «не подтверждено»."""
        try:
            value, _ = self._call(SQL_RESOLVE, (session.sha256,), _flag_validator("valid"))
        except AccountServiceError:
            return False
        return value["valid"] is True

    def _session_operation(self, sql, params, allowed, success, new_session):
        value, committed = self._call(sql, params, _result_validator(allowed))
        result = value["result"]
        if not committed:
            if result == success and self._session_valid_after_unknown_commit(new_session):
                return SessionOutcome(result, session=new_session)
            raise AccountServiceError("retry", "commit_unknown")
        return SessionOutcome(result, session=new_session if result == success else None,
                              retry_after_seconds=value.get("retry_after_seconds") if result == "throttled" else None)

    def _with_bcrypt_slot(self, operation):
        if not self._bcrypt.acquire(blocking=False):
            raise AccountServiceError("overloaded")
        try:
            return operation()
        finally:
            self._bcrypt.release()

    # ---------- регистрация, вход, смена пароля ----------

    def register(self, email_norm, password):
        """-> SessionOutcome(result: created | email_taken | busy | disabled | closed)."""
        self._require_enabled()
        check_email_norm(email_norm)
        proofs = password_proofs(self._settings, password)          # PasswordPolicyError -> 422, без базы
        session = SessionToken.new()
        params = (email_norm, proofs.current, proofs.current_version, session.sha256)
        return self._with_bcrypt_slot(
            lambda: self._session_operation(SQL_REGISTER, params, REGISTER_RESULTS, "created", session))

    def login(self, email_norm, password, previous_session=None):
        """-> SessionOutcome(result: ok | invalid | throttled | busy | disabled). previous_session — сессия из
        cookie запроса: при успехе база отзывает её, если она этого же покупателя (защита от фиксации)."""
        self._require_enabled()
        check_email_norm(email_norm)
        if previous_session is not None:
            _check_session(previous_session)
        proofs = password_proofs(self._settings, password)
        session = SessionToken.new()
        params = (email_norm, proofs.current, proofs.current_version, proofs.previous, proofs.previous_version,
                  session.sha256, previous_session.sha256 if previous_session is not None else None)
        return self._with_bcrypt_slot(
            lambda: self._session_operation(SQL_LOGIN, params, LOGIN_RESULTS, "ok", session))

    def change_password(self, session, current_password, new_password):
        """-> SessionOutcome(result: ok | invalid | session_invalid | throttled | busy | disabled). При успехе
        все сессии покупателя отозваны, возвращается новая; новое доказательство — только текущей версии перца."""
        self._require_enabled()
        _check_session(session)
        current = password_proofs(self._settings, current_password)
        new = password_proofs(self._settings, new_password)
        new_session = SessionToken.new()
        params = (session.sha256, current.current, current.current_version, current.previous,
                  current.previous_version, new.current, new.current_version, new_session.sha256)
        return self._with_bcrypt_slot(
            lambda: self._session_operation(SQL_PASSWORD_CHANGE, params, PASSWORD_CHANGE_RESULTS, "ok", new_session))

    # ---------- сессии ----------

    def _flag(self, sql, session, key):
        self._require_enabled()
        _check_session(session)
        value, committed = self._call(sql, (session.sha256,), _flag_validator(key))
        if not committed:
            raise AccountServiceError("retry", "commit_unknown")
        return value[key]

    def resolve(self, session):
        """Действительна ли сессия (база — источник истины). -> bool."""
        return self._flag(SQL_RESOLVE, session, "valid")

    def touch(self, session):
        """Продлить простой сессии (не чаще интервала из auth_settings). -> bool: продлена ли."""
        return self._flag(SQL_TOUCH, session, "touched")

    def logout(self, session):
        """Отозвать эту сессию. Идемпотентно. Маршрут удаляет cookie при любом исходе (REV5 X6), в том числе
        если здесь брошено AccountServiceError."""
        self._require_enabled()
        _check_session(session)
        _, committed = self._call(SQL_REVOKE, (session.sha256,), lambda value: value == {"result": "ok"})
        if not committed:
            raise AccountServiceError("retry", "commit_unknown")

    def logout_all(self, session):
        """«Выйти на всех устройствах». -> result: ok | session_invalid | busy | disabled."""
        self._require_enabled()
        _check_session(session)
        value, committed = self._call(SQL_REVOKE_ALL, (session.sha256,), _result_validator(REVOKE_ALL_RESULTS))
        if not committed:
            raise AccountServiceError("retry", "commit_unknown")
        return value["result"]

    # ---------- просмотр ----------

    def _view(self, sql, params, expected_type):
        value, committed = self._call(sql, params, lambda v: v is None or isinstance(v, expected_type))
        if not committed:
            raise AccountServiceError("retry", "commit_unknown")
        return value

    def profile(self, session):
        """-> dict (email, created_at) или None, если сессия недействительна."""
        self._require_enabled()
        _check_session(session)
        return self._view(SQL_PROFILE, (session.sha256,), dict)

    def orders(self, session):
        """-> список заказов покупателя или None, если сессия недействительна."""
        self._require_enabled()
        _check_session(session)
        return self._view(SQL_ORDERS, (session.sha256,), list)

    def order(self, session, order_id):
        """-> заказ с составом или None (чужой, несуществующий и при недействительной сессии — одинаково)."""
        self._require_enabled()
        _check_session(session)
        if isinstance(order_id, bool) or not isinstance(order_id, int) or not 0 < order_id <= 2_147_483_647:
            return None
        return self._view(SQL_ORDER, (session.sha256, order_id), dict)


def _result_validator(allowed):
    """{"result": <из allowed>}; у throttled — ещё целое retry_after_seconds >= 1."""
    def validate(value):
        if not isinstance(value, dict) or value.get("result") not in allowed:
            return False
        if value["result"] == "throttled":
            retry_after = value.get("retry_after_seconds")
            return not isinstance(retry_after, bool) and isinstance(retry_after, int) and retry_after >= 1
        return True
    return validate


def _flag_validator(key):
    return lambda value: isinstance(value, dict) and isinstance(value.get(key), bool)


def _quiet_rollback(conn):
    try:
        conn.rollback()
    except psycopg.Error:
        pass


def _quiet_close(conn):
    try:
        conn.close()
    except psycopg.Error:
        pass
