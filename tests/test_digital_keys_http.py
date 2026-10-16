"""PR5: HTTP-слой маршрута выдачи без FastAPI — ограничение частоты и заголовки no-store
(ASGI-middleware вызываются напрямую, как в test_http_security.py)."""

import pytest

import http_security
from http_security import RequestLimitsMiddleware, SensitiveResponseHeadersMiddleware, SlidingWindowLimiter
from test_http_security import _Clock, _call, _echo_app

NO_STORE = {b"cache-control": b"no-store", b"pragma": b"no-cache", b"x-content-type-options": b"nosniff",
            b"referrer-policy": b"no-referrer"}


def _stack():
    limits = RequestLimitsMiddleware(_echo_app, limiter=SlidingWindowLimiter(clock=_Clock()))
    return SensitiveResponseHeadersMiddleware(limits)   # как в api.py: заголовки снаружи ограничителя


def test_delivery_is_limited_to_20_per_minute_with_no_store_on_429():
    app = _stack()
    for _ in range(20):
        status, headers, _ = _call(app, path="/api/v2/orders/5/delivery")
        assert status == 200 and all(headers[k] == v for k, v in NO_STORE.items())
    status, headers, body = _call(app, path="/api/v2/orders/5/delivery")
    assert status == 429 and body["detail"]["code"] == "rate_limited"
    assert all(headers[k] == v for k, v in NO_STORE.items())


def test_no_store_replaces_cache_headers_set_by_the_app():
    async def cached_app(scope, receive, send):
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", b"application/json"), (b"cache-control", b"max-age=600")]})
        await send({"type": "http.response.body", "body": b"{}"})

    status, headers, _ = _call(SensitiveResponseHeadersMiddleware(cached_app), path="/api/v2/orders/1/delivery")
    assert status == 503 and headers[b"cache-control"] == b"no-store"


@pytest.mark.parametrize("path", ["/api/v2/orders/5/status", "/api/v2/products", "/api/v2/orders/5/delivery/x"])
def test_other_paths_are_untouched(path):
    _, headers, _ = _call(SensitiveResponseHeadersMiddleware(_echo_app), path=path)
    assert b"cache-control" not in headers


def test_delivery_rule_is_in_defaults():
    rule = next(r for r in http_security.DEFAULT_RULES if r.name == "order_delivery")
    assert rule.limit == 20 and rule.path.match("/api/v2/orders/12/delivery")
    # Правило выдачи стоит раньше общего лимита "any".
    names = [r.name for r in http_security.DEFAULT_RULES]
    assert names.index("order_delivery") < names.index("any")
