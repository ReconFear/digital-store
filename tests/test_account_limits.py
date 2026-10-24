"""PR6 этап 2d-1: механизм лимитов http_security.py на правилах аккаунтов — офлайн, ASGI напрямую.

Правила и лимит тела — из accounts.web (решение владельцев): регистрация 1/мин на IP, вход 10/мин на IP,
смена пароля 5/мин на IP, вход + регистрация 60/мин на процесс; тело /api/account/* — 4 КиБ. Прежние
лимиты остальных маршрутов (16 КиБ, уведомления — 64 КиБ) не меняются.

В приложении правила аккаунтов применяют маршруты (accounts.routes) после проверки Origin/CSRF — через ту же
http_security.rate_limit_retry, что и middleware; здесь middleware служит только стендом для этой логики.
Лимит тела 4 КиБ — настоящая настройка middleware (path_body_limits).
"""

import json

import pytest

from accounts.web import ACCOUNT_BODY_LIMITS, ACCOUNT_RATE_RULES
from http_security import DEFAULT_RULES, RequestLimitsMiddleware, SlidingWindowLimiter
from test_http_security import _Clock, _call, _echo_app

LOGIN, REGISTER, PASSWORD = "/api/account/login", "/api/account/register", "/api/account/password"


def _app(clock=None):
    return RequestLimitsMiddleware(_echo_app, rules=DEFAULT_RULES + ACCOUNT_RATE_RULES,
                                   path_body_limits=ACCOUNT_BODY_LIMITS,
                                   limiter=SlidingWindowLimiter(clock=clock or _Clock()))


def _ip(n):
    return f"198.51.{n // 250}.{n % 250 + 1}"


# ---------- частота ----------

@pytest.mark.parametrize("path, limit", [(REGISTER, 1), (LOGIN, 10), (PASSWORD, 5)])
def test_per_ip_account_limits(path, limit):
    app = _app()
    for _ in range(limit):
        assert _call(app, path=path, ip=_ip(1))[0] == 200
    status, headers, body = _call(app, path=path, ip=_ip(1))
    assert status == 429 and body["detail"]["code"] == "rate_limited" and int(headers[b"retry-after"]) >= 1
    assert _call(app, path=path, ip=_ip(2))[0] == 200                 # другой адрес не страдает


def test_login_and_register_share_one_per_process_budget_across_addresses():
    clock = _Clock()
    app = _app(clock)
    for n in range(60):                                                # каждый запрос — с нового адреса
        assert _call(app, path=LOGIN if n % 2 else REGISTER, ip=_ip(n))[0] == 200
    for path in (LOGIN, REGISTER):
        status, _, body = _call(app, path=path, ip=_ip(100))
        assert status == 429 and body["detail"]["code"] == "rate_limited"
    assert _call(app, path=PASSWORD, ip=_ip(100))[0] == 200            # смена пароля — вне общего счётчика
    assert _call(app, method="GET", path="/api/account/me", body=b"", ip=_ip(100))[0] == 200
    assert _call(app, path="/api/v2/orders", ip=_ip(100))[0] == 200    # гостевые маршруты не затронуты
    clock.now += 61
    assert _call(app, path=LOGIN, ip=_ip(100))[0] == 200               # окно прошло


def test_requests_refused_per_ip_do_not_spend_the_process_budget():
    """Один адрес, засыпающий регистрацию, не исчерпывает общий счётчик процесса для остальных."""
    app = _app()
    statuses = [_call(app, path=REGISTER, ip=_ip(0))[0] for _ in range(100)]
    assert statuses.count(200) == 1 and statuses.count(429) == 99
    for n in range(1, 60):
        assert _call(app, path=LOGIN, ip=_ip(n))[0] == 200            # 1 + 59 = 60
    assert _call(app, path=LOGIN, ip=_ip(200))[0] == 429


def test_process_rules_are_checked_after_client_rules_and_legacy_order_is_kept():
    default = RequestLimitsMiddleware(_echo_app)
    assert default.rules == DEFAULT_RULES and default.path_body_limits == ()
    combined = _app()
    flags = [rule.per_client for rule in combined.rules]
    assert flags == sorted(flags, reverse=True) and flags.count(False) == 1


# ---------- размер тела ----------

def test_account_bodies_are_limited_to_4_kib():
    app = _app()
    assert _call(app, path=LOGIN, body=b"x" * 4096)[0] == 200
    status, _, body = _call(app, path=PASSWORD, body=b"x" * 4097, ip=_ip(1))
    assert status == 413 and body["detail"]["code"] == "payload_too_large"
    assert _call(app, path="/api/account/logout", chunks=[b"x" * 3000, b"x" * 2000], ip=_ip(2))[0] == 413
    assert _call(app, path="/api/account/logout", chunks=[b"x" * 2000, b"x" * 2096], ip=_ip(3))[0] == 200


def test_other_routes_keep_their_limits():
    app = _app()
    assert _call(app, path="/api/v2/orders", body=b"x" * 16 * 1024)[0] == 200
    assert _call(app, path="/api/v2/orders", body=b"x" * (16 * 1024 + 1), ip=_ip(1))[0] == 413
    assert _call(app, path="/api/accounts", body=b"x" * 10_000)[0] == 200            # не раздел аккаунтов
    webhook = "/api/v2/payments/yookassa/webhook"
    assert _call(app, path=webhook, body=b"x" * 40_000)[0] == 200
    assert _call(app, path=webhook, body=b"x" * (64 * 1024 + 1))[0] == 413
    legacy = RequestLimitsMiddleware(_echo_app, rate_limits=False)                   # без новых параметров
    assert _call(legacy, path=LOGIN, body=b"x" * 10_000)[0] == 200


# ---------- ответы не повторяют введённое ----------

def test_refusals_do_not_echo_request_content():
    marker = "Secret-Password-Marker"
    app = _app()
    payload = json.dumps({"email": "buyer@example.test", "password": marker}).encode()
    too_large = payload + b" " * 5000
    status, headers, body = _call(app, path=LOGIN, body=too_large)
    assert status == 413 and marker not in json.dumps(body, ensure_ascii=False)
    _call(app, path=REGISTER, body=payload, ip=_ip(5))
    status, headers, body = _call(app, path=REGISTER, body=payload, ip=_ip(5))
    assert status == 429 and marker not in json.dumps(body, ensure_ascii=False)
    assert all(marker.encode() not in value for value in headers.values())
