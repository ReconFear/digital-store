"""Сборка HTTP-слоёв приложения (api.py): ограничения запросов, no-store, CORS и аккаунты покупателей PR6.

Одна функция для api.py и офлайн-тестов: порядок слоёв проверяется на той же сборке, что работает в магазине.
Модуль не импортирует database.py и cors_settings.py (оба читают .env при импорте) — параметры передаёт api.py.

Порядок слоёв. В Starlette последний добавленный слой — внешний, а снаружи всех стоит встроенный
ServerErrorMiddleware (его 500 не проходит через пользовательские слои):

  ServerErrorMiddleware
    AccountResponseGuard                — только при включённых аккаунтах, для /api/account* и для заказа v2
                                          с cookie сессии (POST /api/v2/orders, случай B — accounts.order_http;
                                          гостевой заказ без cookie не затрагивается):
                                          no-store на ВСЕХ ответах приложения, в том числе на preflight
                                          и отказах CORS, 404/405, 413/429 и ответах маршрутов; при
                                          непредвиденном исключении — JSON 500 с no-store вместо ответа
                                          ServerErrorMiddleware (исключение идёт дальше — в журнал сервера);
      CORSMiddleware                    — прежние параметры; при включённых аккаунтах — accounts.web
                                          (credentials только точным доверенным https-origin, X-CSRF-Token);
        SensitiveResponseHeadersMiddleware — выдача ключей (PR5), как прежде;
          RequestLimitsMiddleware       — общие лимиты частоты; при включённых аккаунтах ещё тело
                                          /api/account* ≤ 4 КиБ. Лимиты аккаунтов сюда НЕ входят: их применяют
                                          маршруты после проверки Origin, CSRF и входных данных (accounts.routes);
            маршруты приложения и accounts.routes.

При включённых аккаунтах здесь же подключается обработчик RequestValidationError (accounts.order_http): заказ v2
с cookie сессии получает 422 invalid_request без введённых значений, остальные маршруты — стандартный ответ FastAPI.

При выключенных аккаунтах слои и их параметры — ровно как до PR6: маршрутов /api/account нет (404 как у
любого отсутствующего пути), cookie сессии не читается, CORS без credentials, обработчики ошибок FastAPI
стандартные. Файл секретов не открывает уже accounts.config.load_account_settings, которая вызывается до этой
функции.
"""

import json
import logging
import re

from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

from accounts.order_http import AccountOrders, presents_session
from accounts.routes import build_account_router
from accounts.web import ACCOUNT_PATH, load_account_web_config
from http_security import RequestLimitsMiddleware, SENSITIVE_HEADERS, SensitiveResponseHeadersMiddleware

_INTERNAL_ERROR = json.dumps({"detail": {"code": "internal_error", "message": "Внутренняя ошибка."}},
                             ensure_ascii=False).encode("utf-8")
_ANY_PATH = re.compile(r"")


class AccountResponseGuard:
    """Внешний слой раздела аккаунтов и заказа с cookie сессии: no-store на всех ответах и JSON 500 при
    непредвиденном исключении."""

    def __init__(self, app, path=ACCOUNT_PATH, session_request=presents_session):
        self.app = app
        self.protected = SensitiveResponseHeadersMiddleware(app, path=_ANY_PATH)
        self.path = path
        self.session_request = session_request

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not (self.path.match(scope.get("path", "")) or self.session_request(scope)):
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.protected(scope, receive, tracking_send)
        except Exception:
            if not started:
                headers = [(b"content-type", b"application/json"),
                           (b"content-length", str(len(_INTERNAL_ERROR)).encode("ascii"))]
                await send({"type": "http.response.start", "status": 500, "headers": headers + list(SENSITIVE_HEADERS)})
                await send({"type": "http.response.body", "body": _INTERNAL_ERROR})
            raise                                     # ServerErrorMiddleware не отвечает повторно, только журнал


def install_http_stack(app, *, limits, cors_options, account_settings, env, account_service, log=None):
    """Добавляет в app слои HTTP и (при включённых аккаунтах) маршруты аккаунтов. -> AccountWebConfig или None.

    limits — http_security.request_limits_options(); cors_options — cors_settings.cors_middleware_options();
    account_settings — accounts.config.load_account_settings(); env — окружение процесса; account_service —
    фабрика сервиса (AccountService), вызывается только при включённых аккаунтах. Неверная настройка
    аккаунтов — исключение до любого изменения app: приложение не запускается."""
    web = load_account_web_config(account_settings, env, cors_options, limits["trusted_proxies"])
    if web is None:
        app.add_middleware(RequestLimitsMiddleware, **limits)
        app.add_middleware(SensitiveResponseHeadersMiddleware)
        app.add_middleware(CORSMiddleware, **cors_options)
        return None

    router = build_account_router(account_settings, account_service(account_settings), web.trusted_origins,
                                  trusted_proxies=limits["trusted_proxies"], rate_limits=limits["rate_limits"])
    log = log or logging.getLogger("digital_store.accounts")
    for message in web.warnings:
        log.warning(message)
    app.include_router(router)
    app.add_exception_handler(RequestValidationError,
                              AccountOrders(account_settings, web.trusted_origins).validation_error)
    app.add_middleware(RequestLimitsMiddleware, **limits, path_body_limits=web.body_limits)
    app.add_middleware(SensitiveResponseHeadersMiddleware)
    app.add_middleware(CORSMiddleware, **web.cors_options)
    app.add_middleware(AccountResponseGuard)
    return web
