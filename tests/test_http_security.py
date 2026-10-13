"""Ограничение частоты и размера запросов (http_security.py) — без FastAPI и сети.

Middleware вызывается напрямую по протоколу ASGI с минимальным приложением.
Общие тесты API работают с HTTP_RATE_LIMITS=off (conftest.py); здесь ограничитель
включён явно на собственном экземпляре.
"""

import asyncio
import ipaddress
import json

import pytest

import http_security
from http_security import RequestLimitsMiddleware, SlidingWindowLimiter, client_ip, rate_limits_enabled


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


async def _echo_app(scope, receive, send):
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body"):
            break
    payload = json.dumps({"received": len(body)}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": payload})


def _call(app, method="POST", path="/api/v2/orders", body=b"{}", ip="198.51.100.7", headers=None, chunks=None):
    sent = []
    hdrs = [(k.encode(), v.encode()) for k, v in (headers or {}).items()]
    if chunks is None:
        hdrs.append((b"content-length", str(len(body)).encode()))
        chunks = [body]
    messages = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    scope = {"type": "http", "method": method, "path": path, "headers": hdrs, "client": (ip, 5000)}

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    start = sent[0]
    body_out = b"".join(m.get("body", b"") for m in sent[1:])
    return start["status"], dict(start["headers"]), json.loads(body_out)


def _app(clock=None, **kwargs):
    limiter = SlidingWindowLimiter(clock=clock or _Clock())
    return RequestLimitsMiddleware(_echo_app, limiter=limiter, **kwargs)


# ---------- частота ----------

@pytest.mark.parametrize("path, limit", [("/api/v2/orders", 10), ("/orders", 10),
                                         ("/api/v2/orders/5/checkout", 10), ("/api/v2/orders/5/status", 60)])
def test_public_post_routes_are_rate_limited(path, limit):
    app = _app()
    for _ in range(limit):
        assert _call(app, path=path)[0] == 200
    status, headers, body = _call(app, path=path)
    assert status == 429
    assert body["detail"]["code"] == "rate_limited"
    assert int(headers[b"retry-after"]) >= 1


def test_limit_is_per_client_and_window_slides():
    clock = _Clock()
    app = _app(clock)
    for _ in range(10):
        _call(app, ip="198.51.100.1")
    assert _call(app, ip="198.51.100.1")[0] == 429
    assert _call(app, ip="198.51.100.2")[0] == 200     # другой клиент не страдает
    clock.now += 61
    assert _call(app, ip="198.51.100.1")[0] == 200     # окно прошло


def test_order_creation_and_checkout_have_separate_budgets():
    app = _app()
    for _ in range(10):
        _call(app, path="/api/v2/orders")
    assert _call(app, path="/api/v2/orders/1/checkout")[0] == 200


def test_forwarded_header_is_ignored_from_untrusted_peer():
    app = _app()
    for i in range(10):
        _call(app, ip="198.51.100.9", headers={"x-forwarded-for": f"203.0.113.{i}"})
    assert _call(app, ip="198.51.100.9", headers={"x-forwarded-for": "203.0.113.200"})[0] == 429


def test_forwarded_header_is_used_from_trusted_proxy():
    proxies = (ipaddress.ip_network("10.0.0.1/32"),)
    scope = {"client": ("10.0.0.1", 1), "headers": [(b"x-forwarded-for", b"203.0.113.5, 10.0.0.1")]}
    assert client_ip(scope, proxies) == "203.0.113.5"
    scope = {"client": ("10.0.0.1", 1), "headers": [(b"x-forwarded-for", b"garbage")]}
    assert client_ip(scope, proxies) == "10.0.0.1"
    assert client_ip({"client": ("198.51.100.3", 1), "headers": []}, proxies) == "198.51.100.3"


def test_limiter_memory_is_bounded():
    limiter = SlidingWindowLimiter(clock=_Clock(), max_keys=100)
    for i in range(1000):
        limiter.hit(("x", str(i)), 5, 60)
    assert len(limiter._hits) == 100


def test_limits_can_be_disabled_only_explicitly():
    assert rate_limits_enabled({}) is True
    assert rate_limits_enabled({"HTTP_RATE_LIMITS": "off"}) is False
    with pytest.raises(ValueError):
        rate_limits_enabled({"HTTP_RATE_LIMITS": "sometimes"})
    app = _app(rate_limits=False)
    assert all(_call(app)[0] == 200 for _ in range(30))


# ---------- размер ----------

def test_declared_body_over_limit_is_refused_before_reading():
    app = _app()
    status, _, body = _call(app, body=b"x" * (16 * 1024 + 1))
    assert status == 413 and body["detail"]["code"] == "payload_too_large"
    assert _call(app, body=b"x" * (16 * 1024))[0] == 200


def test_chunked_body_over_limit_is_refused():
    app = _app()
    status, _, _ = _call(app, chunks=[b"x" * 10_000, b"x" * 10_000])
    assert status == 413
    status, _, body = _call(app, chunks=[b"x" * 5000, b"y" * 5000])
    assert status == 200 and body["received"] == 10_000


def test_webhook_has_its_own_larger_limit():
    app = _app()
    path = "/api/v2/payments/yookassa/webhook"
    assert _call(app, path=path, body=b"x" * 40_000)[0] == 200
    assert _call(app, path=path, body=b"x" * (64 * 1024 + 1))[0] == 413


def test_bad_content_length_is_refused():
    app = _app()
    assert _call(app, chunks=[b"{}"], headers={"content-length": "-5"})[0] == 400


def test_get_requests_pass_through_untouched():
    app = _app()
    assert _call(app, method="GET", path="/api/v2/products", body=b"")[0] == 200


def test_defaults_cover_every_public_post_route():
    names = {rule.name for rule in http_security.DEFAULT_RULES}
    assert {"orders_create", "checkout", "order_status", "provider_webhook"} <= names
