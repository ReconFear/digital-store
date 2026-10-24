"""Защита изменяющих запросов аккаунтов от CSRF (PR6_DESIGN_REV4 §6.2, REV5 X6, ADDENDUM §3). Без сети и базы.

Три политики для POST (маршруты подключаются на этапе 2d):

  guard_credentials_request  вход и регистрация (сессии ещё нет): разрешённый Origin + Content-Type
                             application/json — защита от login CSRF (REV4 T-H7);
  guard_logout_request       выход (REV5 X6): то же, CSRF-токен НЕ требуется; маршрут удаляет cookie всегда,
                             когда проверка пройдена, независимо от действительности сессии и ответа базы;
  guard_session_request      запросы с сессией (logout-all, смена пароля, заказ с cookie — ADDENDUM §3.1):
                             Origin + Content-Type + X-CSRF-Token этой сессии.

Отказ — RequestRefused: HTTP 403, код "csrf_failed" (ADDENDUM §3.1). Причина (reason) — только для журнала
сервера; в ответ клиенту не идёт, значений заголовков и токенов не содержит.

Origin сравнивается точно: схема, хост (только ASCII, нижний регистр) и порт (по умолчанию 80/443 подставляется)
по множеству разрешённых значений — без шаблонов, префиксов и суффиксов. Нет Origin, "null", несколько значений,
путь, логин, другая схема — отказ.

CSRF-токен = base64url(HMAC-SHA256(csrf_key, SHA-256 токена сессии)) без '=' (REV4 §6.2). Ключ — отдельный
секрет ACCOUNT_CSRF_KEY (accounts.config). Сравнение — hmac.compare_digest.
"""

import base64
import hashlib
import hmac
import ipaddress
import re
from urllib.parse import urlsplit

from accounts.session import SessionToken

CSRF_HEADER = "x-csrf-token"
CSRF_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
CSRF_KEY_BYTES = 32
JSON_MEDIA_TYPE = "application/json"
DEFAULT_PORTS = {"http": 80, "https": 443}
_HOST_RE = re.compile(r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
_ORIGIN_CHARS = re.compile(r"^[A-Za-z0-9.:\[\]/-]+$")


class OriginError(ValueError):
    """Строка не является origin вида схема://хост[:порт]. Сообщение без значения."""


class RequestRefused(Exception):
    """Запрос отклонён проверкой CSRF/Origin. reason — для журнала сервера, не для клиента."""

    http_status = 403
    code = "csrf_failed"

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason

    def as_detail(self):
        return {"code": self.code, "message": "Запрос отклонён проверкой безопасности."}


# ---------- Origin ----------

def parse_origin(value):
    """origin -> (схема, хост, порт) с подставленным портом по умолчанию. Строго: только то, что присылает браузер."""
    if not isinstance(value, str) or not value or value != value.strip() or not _ORIGIN_CHARS.match(value):
        raise OriginError("origin: недопустимые символы или пустое значение")
    try:
        parts = urlsplit(value)
    except ValueError:                                   # например, испорченные скобки IPv6
        raise OriginError("origin: не разбирается как адрес") from None
    if parts.scheme not in DEFAULT_PORTS or parts.netloc == "" or value != f"{parts.scheme}://{parts.netloc}":
        raise OriginError("origin: ожидается схема://хост[:порт] без пути, параметров и логина")
    if parts.username is not None or parts.password is not None:
        raise OriginError("origin: логин недопустим")
    try:
        port = parts.port
    except ValueError:
        raise OriginError("origin: неверный порт") from None
    host = parts.hostname or ""
    if parts.netloc.endswith(":"):
        raise OriginError("origin: пустой порт")
    if host.startswith("[") or ":" in host:
        # IPv6: urlsplit возвращает адрес без скобок.
        try:
            host = f"[{ipaddress.IPv6Address(host).compressed}]"
        except ValueError:
            raise OriginError("origin: неверный IPv6-адрес") from None
    elif not _HOST_RE.match(host):
        raise OriginError("origin: недопустимый хост (только ASCII; IDN — в punycode)")
    if port == 0:
        raise OriginError("origin: неверный порт")
    return parts.scheme, host, port if port is not None else DEFAULT_PORTS[parts.scheme]


class OriginAllowlist:
    """Точное множество разрешённых origin. Неверная запись — ошибка при создании (лучше не запуститься)."""

    __slots__ = ("_origins",)

    def __init__(self, origins):
        if isinstance(origins, str):
            raise TypeError("origins: ожидается последовательность строк, а не строка")
        parsed = set()
        for origin in origins:
            if origin in ("*", "null"):
                raise OriginError("origin: '*' и 'null' запрещены")
            parsed.add(parse_origin(origin))
        self._origins = frozenset(parsed)

    def allows(self, origin):
        try:
            return parse_origin(origin) in self._origins
        except OriginError:
            return False

    def __len__(self):
        return len(self._origins)

    def __repr__(self):
        return f"OriginAllowlist({sorted(f'{s}://{h}:{p}' for s, h, p in self._origins)})"


# ---------- заголовки ----------

def _header_values(headers, name):
    """Все значения заголовка: Starlette Headers (getlist) или Mapping с любым регистром имён."""
    if headers is None:
        return []
    getlist = getattr(headers, "getlist", None)
    if callable(getlist):
        return list(getlist(name))
    values = []
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == name:
            values.extend(value if isinstance(value, (list, tuple)) else [value])
    return values


def _single_header(headers, name, reason):
    values = _header_values(headers, name)
    if not values:
        raise RequestRefused(f"{reason}_missing")
    if len(values) > 1:
        raise RequestRefused(f"{reason}_multiple")
    return values[0]


def require_allowed_origin(headers, allowlist):
    if not isinstance(allowlist, OriginAllowlist):
        raise TypeError("allowlist: ожидается OriginAllowlist")
    origin = _single_header(headers, "origin", "origin")
    if not allowlist.allows(origin):
        raise RequestRefused("origin_forbidden")


def require_json_content_type(headers):
    """Content-Type: application/json (параметры вроде charset допустимы). Иначе межсайтовая форма прошла бы
    без preflight."""
    value = _single_header(headers, "content-type", "content_type")
    if not isinstance(value, str) or value.split(";", 1)[0].strip().lower() != JSON_MEDIA_TYPE:
        raise RequestRefused("content_type_not_json")


# ---------- CSRF ----------

def _check_key(csrf_key):
    if not isinstance(csrf_key, bytes) or len(csrf_key) != CSRF_KEY_BYTES:
        raise TypeError(f"csrf_key: ожидается {CSRF_KEY_BYTES} байта (AccountSettings.csrf_key)")


def csrf_token(csrf_key, session):
    """CSRF-токен сессии для ответа GET /api/account/me (этап 2d)."""
    _check_key(csrf_key)
    if not isinstance(session, SessionToken):
        raise TypeError("session: ожидается SessionToken")
    digest = hmac.new(csrf_key, session.sha256, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_csrf_token(csrf_key, session, provided):
    """True, только если provided — CSRF-токен именно этой сессии. Сравнение за постоянное время."""
    if not isinstance(provided, str) or not CSRF_TOKEN_RE.fullmatch(provided) or not isinstance(session, SessionToken):
        _check_key(csrf_key)
        return False
    return hmac.compare_digest(csrf_token(csrf_key, session).encode("ascii"), provided.encode("ascii"))


def require_csrf(headers, csrf_key, session):
    _check_key(csrf_key)
    if session is None:
        raise RequestRefused("session_missing")
    provided = _single_header(headers, CSRF_HEADER, "csrf")
    if not verify_csrf_token(csrf_key, session, provided):
        raise RequestRefused("csrf_invalid")


# ---------- политики ----------

def guard_credentials_request(headers, allowlist):
    """Вход и регистрация: Origin + Content-Type."""
    require_allowed_origin(headers, allowlist)
    require_json_content_type(headers)


def guard_logout_request(headers, allowlist):
    """Выход (REV5 X6): Origin + Content-Type, без CSRF-токена. После успешной проверки маршрут ВСЕГДА
    удаляет cookie (accounts.session.clear_session_cookie): при действительной, недействительной и
    отсутствующей сессии и при ошибке базы. Межсайтовый выход отклоняется здесь — без удаления cookie."""
    require_allowed_origin(headers, allowlist)
    require_json_content_type(headers)


def guard_session_request(headers, allowlist, csrf_key, session):
    """Запрос с сессией: Origin + Content-Type + CSRF-токен этой сессии."""
    require_allowed_origin(headers, allowlist)
    require_json_content_type(headers)
    require_csrf(headers, csrf_key, session)
