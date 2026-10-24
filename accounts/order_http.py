"""HTTP-часть оформления заказа v2 с cookie сессии (PR6 этап 2e-2; ADDENDUM §3.1–3.3, ADDENDUM2 §2).

POST /api/v2/orders (api.py) различает три случая — до любого обращения к базе:
  * аккаунты выключены — cookie не читается вовсе; маршрут работает ровно как до PR6;
  * cookie сессии нет (случай A) — гостевой заказ PR3/PR5 без изменений: тело {"order", "access_token"},
    прежние заголовки и ошибки, в том числе стандартный 422 FastAPI;
  * cookie есть, даже повреждённая (случай B) — AccountOrders.respond:
      Origin, Content-Type и CSRF (accounts.order_link.order_session) — иначе 403 csrf_failed: заказ не
          создаётся, cookie не трогается (решение В2);
      заказ, секрет доступа и привязка — одна транзакция: create(after_access=link_new_order). Предварительного
          resolve нет — единственная проверка сессии в customer_order_link_new;
      201 — прежние поля и "account_linked": true;
      401 session_invalid и удаление cookie — база отказала в привязке (session_invalid или disabled, решение В1);
          транзакция откатана, гостевого заказа взамен нет;
      ошибки правил заказа (404/409) — то же тело, что у гостя;
      всё прочее (ответ функции вне контракта, ошибка базы, сбой сериализации) — JSON 500 internal_error.
    Все ответы случая B — с Cache-Control: no-store. Ответы слоёв до маршрута (413, 429, 404/405, 500) получают
    no-store во внешнем AccountResponseGuard (http_stack.py) по тому же признаку — presents_session.

Ошибки формата тела (validation_error — обработчик RequestValidationError, подключается только при включённых
аккаунтах). FastAPI проверяет тело раньше маршрута, а стандартный 422 повторяет ввод (поле input). В случае B
сначала та же проверка Origin, Content-Type и CSRF (403 csrf_failed), затем 422 invalid_request без введённых
значений. Остальные маршруты и гостевой заказ — стандартный обработчик FastAPI.

Модуль не импортирует database.py, commerce.orders и cors_settings.py: создание заказа и класс ошибок правил
заказа передаёт api.py.
"""

import logging
import re

from fastapi.exception_handlers import request_validation_exception_handler
from starlette.requests import HTTPConnection

from accounts.config import AccountSettings
from accounts.order_link import OrderLinkError, OrderLinkRefused, link_new_order, order_session
from accounts.request_guard import OriginAllowlist, RequestRefused
from accounts.routes import _error, _response        # формат ответов раздела аккаунтов: no-store, без ввода
from accounts.session import has_session_cookie

ORDER_CREATE_PATH = re.compile(r"^/api/v2/orders/?$")
GUEST_BODY_KEYS = frozenset({"order", "access_token"})

_log = logging.getLogger("digital_store.accounts")


def presents_session(scope):
    """Случай B для слоя ответов: путь оформления заказа v2 и cookie сессии в запросе (даже повреждённая).
    Вызывать только при включённых аккаунтах — при выключенных cookie не читается."""
    return bool(ORDER_CREATE_PATH.match(scope.get("path", ""))) and has_session_cookie(HTTPConnection(scope).cookies)


class AccountOrders:
    """Оформление заказа v2 с cookie сессии. Без состояния; создаётся при запуске (api.py, http_stack.py)."""

    def __init__(self, settings, trusted_origins=None):
        if not isinstance(settings, AccountSettings):
            raise TypeError("settings: ожидается AccountSettings")
        if settings.enabled and (not isinstance(trusted_origins, OriginAllowlist) or not len(trusted_origins)):
            raise TypeError("trusted_origins: нужен непустой OriginAllowlist (ACCOUNT_TRUSTED_ORIGINS)")
        self.settings = settings
        self.trusted_origins = trusted_origins

    def respond(self, request, create, business_errors=()):
        """None — гостевой заказ: маршрут продолжает прежний код. Иначе — готовый ответ случая B.

        create(after_access) — создать заказ (payments.access.create_order_with_access) и вернуть тело гостевого
        ответа {"order", "access_token"}, уже сериализованное. business_errors — исключения правил заказа
        с http_status и as_detail() (commerce.orders.OrderError)."""
        try:
            session = order_session(request, self.settings, self.trusted_origins)
        except RequestRefused as refused:
            _log.info("заказ с сессией: запрос отклонён (%s)", refused.reason)
            return _error(403, "csrf_failed")
        if session is None:
            return None
        try:
            body = create(lambda cursor, order_id: link_new_order(cursor, session, order_id))
            if not isinstance(body, dict) or set(body) != GUEST_BODY_KEYS:
                raise OrderLinkError("тело ответа вне контракта")
        except OrderLinkRefused as refused:
            _log.info("заказ с сессией: привязка отклонена (%s)", refused.result)
            return _error(401, "session_invalid", clear_cookie=True)
        except business_errors as error:
            return _response(error.http_status, {"detail": error.as_detail()})
        except Exception as error:                    # noqa: BLE001 — гостевого заказа взамен нет
            _log.error("заказ с сессией: непредвиденная ошибка (%s)", type(error).__name__)
            return _error(500, "internal_error")
        return _response(201, {**body, "account_linked": True})

    async def validation_error(self, request, exc):
        """Обработчик RequestValidationError приложения. Случай B — 403 или 422 invalid_request без ввода;
        иначе — стандартный ответ FastAPI."""
        if request.scope.get("method") == "POST" and ORDER_CREATE_PATH.match(request.scope.get("path", "")):
            try:
                session = order_session(request, self.settings, self.trusted_origins)
            except RequestRefused as refused:
                _log.info("заказ с сессией: запрос отклонён (%s)", refused.reason)
                return _error(403, "csrf_failed")
            if session is not None:
                return _error(422, "invalid_request")
        return await request_validation_exception_handler(request, exc)
