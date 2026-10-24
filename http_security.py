"""Ограничение частоты запросов и размера тела (чистый ASGI-middleware).

Защищает публичные POST-маршруты (создание заказов, checkout, статус заказа, уведомления
провайдера) от массовых запросов и больших тел. CORS здесь ни при чём: CORS — правило
для браузера, а не защита от прямых HTTP-запросов.

* Размер тела: Content-Length больше лимита -> 413 сразу; тело без Content-Length
  (chunked) читается не дальше лимита -> 413. По умолчанию 16 КиБ, уведомления — 64 КиБ;
  отдельные пути могут получить свой лимит (path_body_limits, например 4 КиБ для аккаунтов PR6).
* Частота: скользящее окно 60 секунд на (правило, IP клиента) -> 429 + Retry-After.
  Правило с per_client=False считает все запросы ПРОЦЕССА одним счётчиком — это лимит процесса,
  а не установки: при N процессах каждый считает сам (до N× limit). Такие правила проверяются после
  правил по IP, чтобы запросы, уже отклонённые лимитом IP, не расходовали общий счётчик процесса.
* IP клиента — адрес TCP-соединения. X-Forwarded-For учитывается, только если соединение
  пришло от доверенного прокси (TRUSTED_PROXY_IPS); иначе заголовок игнорируется,
  чтобы его нельзя было подделать.

Ограничения (см. PR3_NOTES.md): счётчики в памяти одного процесса. При нескольких
процессах uvicorn/gunicorn лимит действует на каждый процесс отдельно и сбрасывается при
перезапуске. Для публичного запуска нужен лимит и на обратном прокси (nginx limit_req).
Выключение: HTTP_RATE_LIMITS=off (только для тестов и локальной разработки).
"""

import ipaddress
import json
import os
import re
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class RateRule:
    name: str
    methods: frozenset
    path: re.Pattern
    limit: int
    window_seconds: int = 60
    # True — счётчик на IP клиента; False — один счётчик на процесс (не на установку, см. описание модуля).
    per_client: bool = True


DEFAULT_RULES = (
    RateRule("orders_create", frozenset({"POST"}), re.compile(r"^/(?:api/v2/)?orders/?$"), 10),
    RateRule("checkout", frozenset({"POST"}), re.compile(r"^/api/v2/orders/[^/]+/checkout/?$"), 10),
    RateRule("order_status", frozenset({"POST"}), re.compile(r"^/api/v2/orders/[^/]+/status/?$"), 60),
    # PR5: получение ключей — 20 в минуту на адрес (каждый показ ещё и пишется в audit_log).
    RateRule("order_delivery", frozenset({"POST"}), re.compile(r"^/api/v2/orders/[^/]+/delivery/?$"), 20),
    RateRule("provider_webhook", frozenset({"POST"}), re.compile(r"^/api/v2/payments/[^/]+/webhook/?$"), 600),
    RateRule("any", frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}),
             re.compile(r"^/"), 600),
)

DEFAULT_MAX_BODY_BYTES = 16 * 1024
WEBHOOK_MAX_BODY_BYTES = 64 * 1024
WEBHOOK_PATH = re.compile(r"^/api/v2/payments/[^/]+/webhook/?$")
BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_TOO_LARGE = {"detail": {"code": "payload_too_large", "message": "Слишком большой запрос."}}
RATE_LIMITED_MESSAGE = "Слишком много запросов. Повторите позже."


def rate_limits_enabled(env=None):
    env = os.environ if env is None else env
    value = env.get("HTTP_RATE_LIMITS", "on").strip().lower()
    if value in ("on", "true", "1", "yes", ""):
        return True
    if value in ("off", "false", "0", "no"):
        return False
    raise ValueError("HTTP_RATE_LIMITS: on или off")


def parse_trusted_proxies(value):
    return tuple(ipaddress.ip_network(p.strip(), strict=False) for p in (value or "").split(",") if p.strip())


def client_ip(scope, trusted_proxies=()):
    """IP клиента. Заголовку X-Forwarded-For верим только от доверенного прокси."""
    peer = (scope.get("client") or ("unknown", 0))[0]
    if not trusted_proxies or not _in_networks(peer, trusted_proxies):
        return peer
    forwarded = []
    for name, value in scope.get("headers") or []:
        if name.lower() == b"x-forwarded-for":
            forwarded.extend(part.strip() for part in value.decode("latin-1").split(","))
    # Справа налево: первый адрес, который не является нашим прокси, — клиент.
    for candidate in reversed([peer] + forwarded):
        if not _in_networks(candidate, trusted_proxies):
            try:
                return str(ipaddress.ip_address(candidate))
            except ValueError:
                return peer
    return peer


def _in_networks(ip, networks):
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(address in net for net in networks)


def ordered_rules(rules):
    """Правила по IP — раньше правил процесса (порядок внутри групп сохраняется): запрос, отклонённый
    лимитом IP, не расходует общий счётчик процесса."""
    return tuple(r for r in rules if r.per_client) + tuple(r for r in rules if not r.per_client)


def rate_limit_retry(limiter, rules, method, path, ip):
    """Через сколько секунд повторить, если запрос превысил правило; None — разрешён и учтён в каждом
    подходящем правиле. rules — в порядке ordered_rules. Общая логика для RequestLimitsMiddleware и
    маршрутов, которые применяют свои правила после собственных проверок (аккаунты PR6)."""
    for rule in rules:
        if method in rule.methods and rule.path.match(path):
            key = (rule.name, ip) if rule.per_client else (rule.name,)
            retry = limiter.hit(key, rule.limit, rule.window_seconds)
            if retry is not None:
                return retry
    return None


class SlidingWindowLimiter:
    def __init__(self, clock=time.monotonic, max_keys=20_000):
        self._clock = clock
        self._max_keys = max_keys
        self._hits = OrderedDict()
        self._lock = threading.Lock()

    def hit(self, key, limit, window):
        """None, если запрос разрешён; иначе — через сколько секунд повторить."""
        now = self._clock()
        with self._lock:
            hits = self._hits.get(key)
            if hits is None:
                hits = self._hits[key] = deque()
                while len(self._hits) > self._max_keys:
                    self._hits.popitem(last=False)
            else:
                self._hits.move_to_end(key)
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return max(1, int(hits[0] + window - now) + 1)
            hits.append(now)
            return None


async def _send_json(send, status, body, extra_headers=()):
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    headers.extend(extra_headers)
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


class RequestLimitsMiddleware:
    def __init__(self, app, *, rules=DEFAULT_RULES, max_body_bytes=DEFAULT_MAX_BODY_BYTES,
                 webhook_max_body_bytes=WEBHOOK_MAX_BODY_BYTES, path_body_limits=(), trusted_proxies=(),
                 rate_limits=True, limiter=None):
        self.app = app
        self.rules = ordered_rules(rules)
        self.max_body_bytes = max_body_bytes
        self.webhook_max_body_bytes = webhook_max_body_bytes
        # ((шаблон пути, байт), ...) — первый совпавший; иначе лимит уведомлений или общий.
        self.path_body_limits = tuple(path_body_limits)
        self.trusted_proxies = trusted_proxies
        self.rate_limits = rate_limits
        self.limiter = limiter or SlidingWindowLimiter()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = scope.get("method", "GET")
        path = scope.get("path", "/")

        if self.rate_limits:
            retry = rate_limit_retry(self.limiter, self.rules, method, path, client_ip(scope, self.trusted_proxies))
            if retry is not None:
                await _send_json(send, 429, {"detail": {"code": "rate_limited", "message": RATE_LIMITED_MESSAGE}},
                                 [(b"retry-after", str(retry).encode())])
                return

        if method not in BODY_METHODS:
            await self.app(scope, receive, send)
            return

        limit = self._body_limit(path)
        declared = None
        for name, value in scope.get("headers") or []:
            if name.lower() == b"content-length":
                if not value.isdigit():
                    await _send_json(send, 400, {"detail": {"code": "bad_request", "message": "Bad Content-Length"}})
                    return
                declared = int(value)
        if declared is not None and declared > limit:
            await _send_json(send, 413, _TOO_LARGE)
            return

        # Тело читается целиком, но не дальше лимита, и передаётся приложению из буфера.
        chunks, size, more = [], 0, True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > limit:
                await _send_json(send, 413, _TOO_LARGE)
                return
            chunks.append(chunk)
            more = message.get("more_body", False)
        body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        await self.app(scope, replay, send)

    def _body_limit(self, path):
        for pattern, limit in self.path_body_limits:
            if pattern.match(path):
                return limit
        return self.webhook_max_body_bytes if WEBHOOK_PATH.match(path) else self.max_body_bytes


# Ответы с секретами покупателя (ключи товара) не должны оседать в кэшах браузера и прокси.
SENSITIVE_PATH = re.compile(r"^/api/v2/orders/[^/]+/delivery/?$")
SENSITIVE_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"pragma", b"no-cache"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)


class SensitiveResponseHeadersMiddleware:
    """Добавляет запрет кэширования ко ВСЕМ ответам маршрута выдачи (200, 404, 422, 429, 503, 500)."""

    def __init__(self, app, path=SENSITIVE_PATH):
        self.app = app
        self.path = path

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.path.match(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        names = {name for name, _ in SENSITIVE_HEADERS}

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = [(n, v) for n, v in message.get("headers", []) if n.lower() not in names]
                headers.extend(SENSITIVE_HEADERS)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def request_limits_options(env=None):
    env = os.environ if env is None else env
    return {
        "rate_limits": rate_limits_enabled(env),
        "trusted_proxies": parse_trusted_proxies(env.get("TRUSTED_PROXY_IPS", "")),
    }
