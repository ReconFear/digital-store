"""Привязка НОВОГО заказа к аккаунту покупателя (PR6 этап 2e; ADDENDUM §3.1–3.3, ADDENDUM2 §2, §3).

Маршрут создания заказа v2 подключает это на этапе 2e-2; здесь — только две проверяемые части:

  order_session(request, settings, trusted_origins)
      None  — гостевой заказ: аккаунты выключены (cookie не читается вовсе, ADDENDUM2 §2) или cookie сессии
              в запросе нет (случай A — поведение PR3/PR5 без изменений, без проверок CSRF);
      SessionToken — cookie есть (случай B) и запрос прошёл Origin, Content-Type: application/json и CSRF-токен
              этой сессии (accounts.request_guard.guard_session_request). Иначе RequestRefused → 403 csrf_failed:
              заказ не создаётся, cookie не удаляется — в том числе при повреждённом значении cookie (решение В2).
      Действительность сессии здесь НЕ проверяется и отдельной транзакцией не уточняется: окончательная
      проверка — внутри customer_order_link_new, в транзакции заказа, под FOR SHARE на строке покупателя.

  link_new_order(cursor, session, order_id)
      Шаг транзакции создания заказа — после order_access_issue (PR3), до COMMIT
      (payments.access.create_order_with_access(..., after_access=...)). В базу уходит только SHA-256 токена.
      'linked' — заказ привязан; 'session_invalid' и 'disabled' — OrderLinkRefused (маршрут: 401 session_invalid
      и удаление cookie, решение В1); ответ вне контракта — OrderLinkError. Любое исключение здесь прерывает
      транзакцию заказа: заказ, позиции и секрет доступа откатываются — гостевого заказа взамен не создаётся.

Модуль не обращается к базе сам (только через переданный курсор) и не импортирует database.py.
"""

from accounts.config import AccountSettings
from accounts.request_guard import OriginAllowlist, guard_session_request
from accounts.session import SessionToken, has_session_cookie, session_from_cookies

SQL_LINK_NEW_ORDER = "SELECT customer_order_link_new(%s, %s)"
LINK_RESULTS = frozenset({"linked", "session_invalid", "disabled"})
MAX_ORDER_ID = 2_147_483_647


class OrderLinkRefused(Exception):
    """База отказала в привязке (сессия недействительна или аккаунты выключены в базе). result — её ответ."""

    def __init__(self, result):
        super().__init__(result)
        self.result = result


class OrderLinkError(Exception):
    """Ответ customer_order_link_new вне контракта. Значения ответа в сообщение не включаются."""


def order_session(request, settings, trusted_origins):
    """Сессия для привязки создаваемого заказа или None (гостевой заказ). RequestRefused — отказ 403."""
    if not isinstance(settings, AccountSettings):
        raise TypeError("settings: ожидается AccountSettings")
    if not settings.enabled:
        return None                                   # cookie не читается вовсе
    if not isinstance(trusted_origins, OriginAllowlist) or not len(trusted_origins):
        raise TypeError("trusted_origins: нужен непустой OriginAllowlist (ACCOUNT_TRUSTED_ORIGINS)")
    cookies = request.cookies
    if not has_session_cookie(cookies):
        return None                                   # случай A: гость, как до PR6
    session = session_from_cookies(cookies)           # повреждённое значение -> None -> отказ CSRF ниже
    guard_session_request(request.headers, trusted_origins, settings.csrf_key, session)
    return session


def link_new_order(cursor, session, order_id):
    """Привязать заказ, созданный в текущей транзакции курсора. Исключение — отказ (транзакцию откатить)."""
    if not isinstance(session, SessionToken):
        raise TypeError("session: ожидается SessionToken")
    if isinstance(order_id, bool) or not isinstance(order_id, int) or not 0 < order_id <= MAX_ORDER_ID:
        raise ValueError("order_id: ожидается id созданного заказа")
    cursor.execute(SQL_LINK_NEW_ORDER, (session.sha256, order_id))
    row = cursor.fetchone()
    value = row[0] if row else None
    if not isinstance(value, dict) or set(value) != {"result"} or value["result"] not in LINK_RESULTS:
        raise OrderLinkError("customer_order_link_new: ответ вне контракта")
    if value["result"] != "linked":
        raise OrderLinkRefused(value["result"])
