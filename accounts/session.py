"""Сессия покупателя: токен, его хэш для базы и cookie (PR6_DESIGN_REV4 §6.1). Без сети и базы.

  токен    secrets.token_urlsafe(32) — 43 символа base64url, 256 бит; знает только браузер покупателя;
  в базу   только SHA-256 токена (customer_sessions.token_sha256, 32 байта) — параметром запроса, сам токен
           в SQL, журналы и сообщения не попадает;
  cookie   __Host-ds_session=<токен>; Max-Age=…; Path=/; Secure; HttpOnly; SameSite=Strict — без Domain.
           Префикс __Host- браузер принимает только с Secure, Path=/ и без Domain, поэтому соседний поддомен
           не может подложить свою cookie с этим именем. Атрибуты одинаковы везде, в том числе для localhost:
           послаблений для разработки здесь нет (вопрос Б6 не решён).
  удаление тот же набор атрибутов, пустое значение, Max-Age=0 и Expires в прошлом.

Cookie выставляется только после подтверждённого COMMIT (REV4 §2.1, REV5 §6.2) — это обязанность
маршрута (этап 2d); здесь только построение заголовков. Действительность сессии решает база при каждом
запросе (auth_session_resolve); Max-Age cookie — лишь верхняя граница её хранения браузером.

SessionToken не показывает токен в repr/str и f-строках: значение берётся только явно (.value) для Set-Cookie.
"""

import hashlib
import re
import secrets

SESSION_COOKIE_NAME = "__Host-ds_session"
SESSION_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
# Абсолютный срок сессии по умолчанию в базе (auth_settings.session_absolute = 90 дней). Веб-роль настроек
# не читает; если в базе срок короче, cookie просто переживёт сессию и будет отклонена resolve.
SESSION_COOKIE_MAX_AGE = 90 * 24 * 60 * 60
MAX_COOKIE_MAX_AGE = 366 * 24 * 60 * 60
_COOKIE_ATTRIBUTES = "Path=/; Secure; HttpOnly; SameSite=Strict"
_EXPIRED = "Thu, 01 Jan 1970 00:00:00 GMT"


class SessionToken:
    """Токен сессии. repr/str скрывают значение; .value — только для Set-Cookie, .sha256 — для базы."""

    __slots__ = ("_value",)

    def __init__(self, value):
        if not isinstance(value, str) or not SESSION_TOKEN_RE.fullmatch(value):
            raise ValueError("токен сессии: ожидается 43 символа base64url")
        self._value = value

    @classmethod
    def new(cls):
        return cls(secrets.token_urlsafe(32))

    @property
    def value(self):
        return self._value

    @property
    def sha256(self):
        """32 байта для параметров функций базы (p_session, p_new_session и т. п.)."""
        return hashlib.sha256(self._value.encode("ascii")).digest()

    def __repr__(self):
        return "SessionToken(<скрыт>)"

    __str__ = __repr__

    def __format__(self, spec):
        return repr(self)

    def __reduce__(self):
        raise TypeError("SessionToken не сериализуется")


def session_from_cookies(cookies):
    """Токен из разобранных cookie запроса (Mapping, например request.cookies) или None.

    None — cookie нет или значение не похоже на токен (такое значение в базу не передаётся)."""
    value = cookies.get(SESSION_COOKIE_NAME) if cookies is not None else None
    if not isinstance(value, str) or not SESSION_TOKEN_RE.fullmatch(value):
        return None
    return SessionToken(value)


def has_session_cookie(cookies):
    """Пришла ли cookie сессии вообще (даже с неверным значением) — для разделения гостевого запроса и
    запроса аккаунта (PR6_DESIGN_REV5_ADDENDUM §3.1)."""
    return cookies is not None and SESSION_COOKIE_NAME in cookies


def session_cookie_header(token, max_age=SESSION_COOKIE_MAX_AGE):
    """Значение заголовка Set-Cookie для новой сессии."""
    if not isinstance(token, SessionToken):
        raise TypeError("token: ожидается SessionToken")
    if isinstance(max_age, bool) or not isinstance(max_age, int) or not 0 < max_age <= MAX_COOKIE_MAX_AGE:
        raise ValueError(f"max_age: целое число секунд от 1 до {MAX_COOKIE_MAX_AGE}")
    return f"{SESSION_COOKIE_NAME}={token.value}; Max-Age={max_age}; {_COOKIE_ATTRIBUTES}"


def clear_session_cookie_header():
    """Значение заголовка Set-Cookie, удаляющего cookie сессии (те же Path/Secure/SameSite, иначе браузер
    не сопоставит удаление с cookie __Host-)."""
    return f"{SESSION_COOKIE_NAME}=; Max-Age=0; Expires={_EXPIRED}; {_COOKIE_ATTRIBUTES}"


def set_session_cookie(response, token, max_age=SESSION_COOKIE_MAX_AGE):
    """Добавляет Set-Cookie к ответу Starlette/FastAPI (response.headers.append). Вызывать после COMMIT."""
    response.headers.append("set-cookie", session_cookie_header(token, max_age))


def clear_session_cookie(response):
    response.headers.append("set-cookie", clear_session_cookie_header())
