"""HTTP-маршруты аккаунтов покупателей (PR6 этап 2d-2): фабрика APIRouter с внедряемым сервисом и настройками.

Подключение в api.py — этап 2d-3; привязка заказа к аккаунту (POST /api/v2/orders с cookie) — этап 2e.
Модуль не импортирует database.py и cors_settings.py (оба читают .env при импорте).

Порядок защиты изменяющего запроса:
  1. общие лимиты и размер тела — RequestLimitsMiddleware снаружи (api.py, 2d-3); здесь размер тела
     проверяется ещё раз (4 КиБ);
  2. Origin из ACCOUNT_TRUSTED_ORIGINS и Content-Type: application/json (accounts.request_guard);
  3. CSRF-токен сессии — для logout-all и смены пароля; вход, регистрация и выход (REV5 X6) — без него.
     CSRF проверяется РАНЬШЕ лимитов аккаунтов: проверка без базы (HMAC), 256-битный токен не подобрать,
     а запрос с чужим или устаревшим токеном не должен расходовать квоту владельца сессии;
  4. разбор JSON и проверка полей: email — только ASCII (accounts.email), пароль — правила Stage 2a;
  5. лимиты аккаунтов (accounts.web.ACCOUNT_RATE_RULES) — учитываются только запросы, прошедшие 2–4;
  6. AccountService — единственное обращение к базе.

GET /me, /orders, /orders/{id} только читают и требуют действительную сессию; Origin не проверяется
(межсайтовое чтение закрывает CORS, а cookie SameSite=Strict межсайтово не отправляется).

Ответы: {"detail": {"code", "message"[, "field"]}} без введённых значений, SQL, SQLSTATE и diagnostic
(они — только в журнал). Свой разбор JSON вместо моделей FastAPI: стандартный ответ 422 повторяет ввод.
Все ответы маршрутов — с Cache-Control: no-store. Cookie сессии ставится только по SessionOutcome.session
(есть лишь после подтверждённого COMMIT); любой 401 session_invalid и успешный выход её удаляют.
Аккаунты выключены — маршрутов нет вовсе: 404 как у несуществующего пути, cookie не читается.
"""

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from accounts.config import AccountSettings
from accounts.email import normalize_email
from accounts.proof import PasswordPolicyError, normalize_password
from accounts.request_guard import (
    OriginAllowlist, RequestRefused, csrf_token, guard_credentials_request, guard_logout_request,
    guard_session_request,
)
from accounts.service import AccountInputError, AccountServiceError, SessionOutcome
from accounts.session import SessionToken, clear_session_cookie, session_from_cookies, set_session_cookie
from accounts.web import ACCOUNT_MAX_BODY_BYTES, ACCOUNT_RATE_RULES
from http_security import (
    RATE_LIMITED_MESSAGE, SENSITIVE_HEADERS, SlidingWindowLimiter, client_ip, ordered_rules, rate_limit_retry,
)

PREFIX = "/api/account"
REGISTER_PATH = PREFIX + "/register"
LOGIN_PATH = PREFIX + "/login"
LOGOUT_PATH = PREFIX + "/logout"
LOGOUT_ALL_PATH = PREFIX + "/logout-all"
PASSWORD_PATH = PREFIX + "/password"
ME_PATH = PREFIX + "/me"
ORDERS_PATH = PREFIX + "/orders"
ORDER_PATH = ORDERS_PATH + "/{order_id}"

PROFILE_KEYS = ("email", "created_at")
ORDER_KEYS = ("id", "status", "created_at", "total_minor", "currency")
ITEM_KEYS = ("product_id", "name", "product_type", "quantity", "unit_price_minor", "line_total_minor")
MAX_ORDER_ID = 2_147_483_647

_log = logging.getLogger("digital_store.accounts")
_NO_STORE = {name.decode("ascii"): value.decode("ascii") for name, value in SENSITIVE_HEADERS}
_NOT_FOUND = {"detail": "Not Found"}                 # тело FastAPI для несуществующего маршрута
_MESSAGES = {
    "invalid_request": "Неверный формат запроса.",
    "payload_too_large": "Слишком большой запрос.",
    "email_invalid": "Неверный адрес электронной почты.",
    "email_reserved": "Этот адрес нельзя использовать.",
    "password_invalid": "Неверный пароль.",
    "password_forbidden_character": "Пароль содержит недопустимый символ.",
    "password_too_short": "Пароль слишком короткий.",
    "password_too_long": "Пароль слишком длинный.",
    "email_taken": "Аккаунт с этим адресом уже есть.",
    "registration_closed": "Регистрация временно закрыта.",
    "invalid_credentials": "Неверный адрес или пароль.",
    "invalid_password": "Неверный текущий пароль.",
    "session_invalid": "Нужно войти в аккаунт.",
    "csrf_failed": "Запрос отклонён проверкой безопасности.",
    "rate_limited": RATE_LIMITED_MESSAGE,
    "busy": "Сервис занят. Повторите через несколько секунд.",
    "service_unavailable": "Сервис временно недоступен. Повторите позже.",
    "internal_error": "Внутренняя ошибка.",
    "order_not_found": "Заказ не найден.",
}
_LOGIN_REFUSALS = {"invalid": (401, "invalid_credentials")}
_REGISTER_REFUSALS = {"email_taken": (409, "email_taken"), "closed": (403, "registration_closed")}
_PASSWORD_REFUSALS = {"invalid": (403, "invalid_password")}


# ---------- ответы ----------

class _Refusal(Exception):
    """Отказ, уже выраженный в HTTP. Введённых значений не содержит."""

    def __init__(self, status, code, *, field=None, retry_after=None, clear_cookie=False):
        super().__init__(code)
        self.status, self.code, self.field = status, code, field
        self.retry_after, self.clear_cookie = retry_after, clear_cookie

    def response(self):
        return _error(self.status, self.code, field=self.field, retry_after=self.retry_after,
                      clear_cookie=self.clear_cookie)


def _response(status, content, headers=None):
    return JSONResponse(status_code=status, content=content, headers={**_NO_STORE, **(headers or {})})


def _error(status, code, *, field=None, retry_after=None, clear_cookie=False):
    detail = {"code": code, "message": _MESSAGES.get(code, _MESSAGES["invalid_request"])}
    if field is not None:
        detail["field"] = field
    response = _response(status, {"detail": detail},
                         {"Retry-After": str(retry_after)} if retry_after is not None else None)
    if clear_cookie:
        clear_session_cookie(response)
    return response


def _not_found():
    """Аккаунты выключены в базе: как несуществующий маршрут (и как выключенные в Python)."""
    return _response(404, _NOT_FOUND)


def _service_failure(error):
    _log.warning("аккаунты: ошибка сервиса %s (%s)", error.code, error.diagnostic)
    if error.code == "disabled":
        return _not_found()
    if error.code == "overloaded":
        return _error(503, "busy", retry_after=1)
    if error.code == "retry":
        return _error(503, "service_unavailable", retry_after=1)
    return _error(500, "internal_error")


def _contract_violation(what):
    _log.error("аккаунты: ответ сервиса вне контракта (%s)", what)
    return _Refusal(500, "internal_error")


def _pick(value, keys, what):
    """Только известные поля: изменение функции базы не выведет наружу лишнее."""
    if not isinstance(value, dict) or any(key not in value for key in keys):
        raise _contract_violation(what)
    return {key: value[key] for key in keys}


def _session_response(outcome, success, status, refusals, what):
    """SessionOutcome -> ответ. Cookie — только при успехе с сессией (её сервис выдаёт после COMMIT)."""
    if not isinstance(outcome, SessionOutcome):
        raise _contract_violation(what)
    if outcome.result == success:
        if not isinstance(outcome.session, SessionToken):
            raise _contract_violation(what)
        response = _response(status, {"result": success})
        set_session_cookie(response, outcome.session)
        return response
    if outcome.result in refusals:
        return _error(*refusals[outcome.result])
    retry_after = outcome.retry_after_seconds
    if outcome.result == "throttled" and type(retry_after) is int and retry_after >= 1:
        return _error(429, "rate_limited", retry_after=retry_after)
    if outcome.result == "busy":
        return _error(503, "busy", retry_after=1)
    if outcome.result == "session_invalid":
        return _error(401, "session_invalid", clear_cookie=True)
    if outcome.result == "disabled":
        return _not_found()
    raise _contract_violation(what)


async def _handled(run):
    try:
        return await run()
    except _Refusal as refusal:
        return refusal.response()
    except RequestRefused as refused:
        _log.info("аккаунты: запрос отклонён (%s)", refused.reason)
        return _error(403, "csrf_failed")
    except AccountInputError as error:
        return _error(422, error.code, field="email")
    except PasswordPolicyError as error:
        return _error(422, error.code)
    except AccountServiceError as error:
        return _service_failure(error)
    except Exception as error:                        # noqa: BLE001 — иначе 500 Starlette без no-store
        _log.error("аккаунты: непредвиденная ошибка (%s)", type(error).__name__)
        return _error(500, "internal_error")


# ---------- входные данные ----------

async def _json_object(request):
    body = await request.body()
    if len(body) > ACCOUNT_MAX_BODY_BYTES:
        raise _Refusal(413, "payload_too_large")
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise _Refusal(422, "invalid_request") from None
    if not isinstance(data, dict):
        raise _Refusal(422, "invalid_request")
    return data                                       # лишние поля (customer_id и т. п.) не читаются — T-H6


def _email_field(data):
    try:
        return normalize_email(data.get("email"))
    except AccountInputError as error:
        raise _Refusal(422, error.code, field="email") from None


def _password_field(data, field):
    """Правила пароля — до лимитов (опечатка не расходует квоту); доказательство строит сервис."""
    value = data.get(field)
    try:
        normalize_password(value)
    except PasswordPolicyError as error:
        raise _Refusal(422, error.code, field=field) from None
    return value


def _order_number(text):
    if not (isinstance(text, str) and text.isascii() and text.isdigit() and len(text) <= 10):
        return None
    number = int(text)
    return number if 0 < number <= MAX_ORDER_ID else None


# ---------- маршруты ----------

def build_account_router(settings, service=None, trusted_origins=None, *, trusted_proxies=(), rate_limits=True,
                         limiter=None, rules=ACCOUNT_RATE_RULES):
    """APIRouter раздела /api/account. При выключенных аккаунтах — пустой (маршрутов нет)."""
    if not isinstance(settings, AccountSettings):
        raise TypeError("settings: ожидается AccountSettings")
    router = APIRouter()
    if not settings.enabled:
        return router
    if service is None:
        raise TypeError("service: нужен AccountService")
    if not isinstance(trusted_origins, OriginAllowlist) or not len(trusted_origins):
        raise TypeError("trusted_origins: нужен непустой OriginAllowlist (ACCOUNT_TRUSTED_ORIGINS)")
    csrf_key = settings.csrf_key
    rules = ordered_rules(rules)
    limiter = limiter or SlidingWindowLimiter()

    def check_limits(request):
        if not rate_limits:
            return
        ip = client_ip(request.scope, trusted_proxies)
        retry = rate_limit_retry(limiter, rules, request.method, request.scope["path"], ip)
        if retry is not None:
            raise _Refusal(429, "rate_limited", retry_after=retry)

    def valid_cookie_session(request):
        session = session_from_cookies(request.cookies)
        if session is None:
            raise _Refusal(401, "session_invalid", clear_cookie=True)
        return session

    async def call(method, *args):
        # Синхронный сервис — в пуле потоков; обрыв соединения клиентом не прерывает транзакцию (Г9).
        return await run_in_threadpool(method, *args)

    @router.post(REGISTER_PATH)
    async def register(request: Request):
        async def run():
            guard_credentials_request(request.headers, trusted_origins)
            data = await _json_object(request)
            email, password = _email_field(data), _password_field(data, "password")
            check_limits(request)
            outcome = await call(service.register, email, password)
            return _session_response(outcome, "created", 201, _REGISTER_REFUSALS, "register")
        return await _handled(run)

    @router.post(LOGIN_PATH)
    async def login(request: Request):
        async def run():
            guard_credentials_request(request.headers, trusted_origins)
            data = await _json_object(request)
            email, password = _email_field(data), _password_field(data, "password")
            check_limits(request)
            previous = session_from_cookies(request.cookies)          # отзывается базой, если того же покупателя
            outcome = await call(service.login, email, password, previous)
            return _session_response(outcome, "ok", 200, _LOGIN_REFUSALS, "login")
        return await _handled(run)

    @router.post(LOGOUT_PATH)
    async def logout(request: Request):
        try:
            guard_logout_request(request.headers, trusted_origins)
        except RequestRefused as refused:
            _log.info("аккаунты: запрос отклонён (%s)", refused.reason)
            return _error(403, "csrf_failed")                         # межсайтовый выход: cookie не трогается
        session = session_from_cookies(request.cookies)
        response = _response(200, {"result": "ok"})
        if session is not None:
            try:
                await call(service.logout, session)
            except AccountServiceError as error:
                response = _service_failure(error)
            except Exception as error:                                # noqa: BLE001 — cookie удаляется всегда
                _log.error("аккаунты: сбой выхода (%s)", type(error).__name__)
                response = _error(500, "internal_error")
        clear_session_cookie(response)                                # REV5 X6: при любом исходе
        return response

    @router.post(LOGOUT_ALL_PATH)
    async def logout_all(request: Request):
        async def run():
            session = session_from_cookies(request.cookies)
            guard_session_request(request.headers, trusted_origins, csrf_key, session)
            result = await call(service.logout_all, session)
            if result == "ok":
                response = _response(200, {"result": "ok"})
                clear_session_cookie(response)
                return response
            if result == "session_invalid":
                return _error(401, "session_invalid", clear_cookie=True)
            if result == "busy":
                return _error(503, "busy", retry_after=1)
            if result == "disabled":
                return _not_found()
            raise _contract_violation("logout-all")
        return await _handled(run)

    @router.post(PASSWORD_PATH)
    async def change_password(request: Request):
        async def run():
            session = session_from_cookies(request.cookies)
            guard_session_request(request.headers, trusted_origins, csrf_key, session)
            data = await _json_object(request)
            current = _password_field(data, "current_password")
            new = _password_field(data, "new_password")
            check_limits(request)
            outcome = await call(service.change_password, session, current, new)
            return _session_response(outcome, "ok", 200, _PASSWORD_REFUSALS, "password")
        return await _handled(run)

    @router.get(ME_PATH)
    async def me(request: Request):
        async def run():
            session = valid_cookie_session(request)
            profile = await call(service.profile, session)
            if profile is None:
                raise _Refusal(401, "session_invalid", clear_cookie=True)
            body = _pick(profile, PROFILE_KEYS, "me")
            try:
                await call(service.touch, session)                    # продление простоя — не условие ответа
            except AccountServiceError as error:
                _log.info("аккаунты: продление сессии не выполнено (%s)", error.code)
            body["csrf_token"] = csrf_token(csrf_key, session)
            return _response(200, body)
        return await _handled(run)

    @router.get(ORDERS_PATH)
    async def orders(request: Request):
        async def run():
            session = valid_cookie_session(request)
            listing = await call(service.orders, session)
            if listing is None:
                raise _Refusal(401, "session_invalid", clear_cookie=True)
            if not isinstance(listing, list):
                raise _contract_violation("orders")
            return _response(200, {"orders": [_pick(item, ORDER_KEYS, "orders") for item in listing]})
        return await _handled(run)

    @router.get(ORDER_PATH)
    async def order(request: Request, order_id: str):
        async def run():
            session = valid_cookie_session(request)
            number = _order_number(order_id)
            view = await call(service.order, session, number) if number is not None else None
            if view is None:
                # Чужой, несуществующий и невозможный id — одинаковый 404; недействительная сессия — 401.
                if await call(service.resolve, session) is not True:
                    raise _Refusal(401, "session_invalid", clear_cookie=True)
                return _error(404, "order_not_found")
            if not isinstance(view, dict) or not isinstance(view.get("items"), list):
                raise _contract_violation("order")
            return _response(200, {"order": _pick(view.get("order"), ORDER_KEYS, "order"),
                                   "items": [_pick(item, ITEM_KEYS, "order") for item in view["items"]]})
        return await _handled(run)

    return router
