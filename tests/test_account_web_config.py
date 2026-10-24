"""PR6 этап 2d-1: конфигурация HTTP-слоя аккаунтов (accounts/web.py) — офлайн.

Окружение передаётся словарём; cors_settings.py не импортируется (он читает .env при импорте), его константы
берутся из исходного текста (ast). CORS проверяется на настоящем CORSMiddleware Starlette с параметрами,
которые выдаёт accounts.web: credentials получает только точный доверенный https-origin.
"""

import ast
import ipaddress
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from accounts.config import AccountConfigError, AccountSettings, Pepper
from accounts.request_guard import OriginAllowlist
from accounts.web import (
    ACCOUNT_BODY_LIMITS, ACCOUNT_MAX_BODY_BYTES, ACCOUNT_PATH, ACCOUNT_RATE_RULES, AccountWebConfigError,
    account_cors_options, account_startup_warnings, load_account_web_config, parse_trusted_origins,
)

PROJECT_DIR = Path(__file__).resolve().parent.parent
SHOP = "https://shop.example.test"
OTHER_TRUSTED = "https://admin.example.test:8443"
ENABLED = AccountSettings(enabled=True, pepper_current=Pepper(1, bytes(range(32))), csrf_key=bytes(range(64, 96)))
PROXIES = (ipaddress.ip_network("10.0.0.1/32"),)
# Адреса, которым credentials НЕ положены: похожие хосты, другой порт и схема, null.
UNTRUSTED = ("https://evil.example.test", "https://shop.example.test.evil.test", "https://evilshop.example.test",
             "https://sub.shop.example.test", "http://shop.example.test", "https://shop.example.test:8443", "null")


def _cors_settings_constants():
    tree = ast.parse((PROJECT_DIR / "cors_settings.py").read_text(encoding="utf-8"))
    values = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in ("ALLOWED_METHODS", "ALLOWED_HEADERS", "PREFLIGHT_MAX_AGE"):
                values[node.targets[0].id] = ast.literal_eval(node.value)
    return values


def _base(origins):
    """Как cors_settings.cors_middleware_options() для заданного списка origin."""
    constants = _cors_settings_constants()
    return {"allow_origins": list(origins), "allow_credentials": False,
            "allow_methods": list(constants["ALLOWED_METHODS"]), "allow_headers": list(constants["ALLOWED_HEADERS"]),
            "max_age": constants["PREFLIGHT_MAX_AGE"]}


class _Untouchable(dict):
    def __getitem__(self, key):
        raise AssertionError("окружение прочитано при выключенных аккаунтах")

    get = __contains__ = __getitem__


# ---------- выключенные аккаунты ----------

def test_disabled_accounts_read_no_environment():
    assert load_account_web_config(AccountSettings(), _Untouchable(), _base([]), ()) is None
    with pytest.raises(TypeError):
        load_account_web_config(None, {}, _base([]), ())


# ---------- ACCOUNT_TRUSTED_ORIGINS ----------

def test_trusted_origins_are_an_exact_https_allowlist():
    trusted = parse_trusted_origins(f" {SHOP} ,{OTHER_TRUSTED}, https://[::1]:8443 ,")
    assert isinstance(trusted, OriginAllowlist) and len(trusted) == 3
    assert trusted.allows(SHOP) and trusted.allows(SHOP + ":443") and trusted.allows(OTHER_TRUSTED)
    assert trusted.allows("https://[0:0:0:0:0:0:0:1]:8443")
    assert trusted.allows("https://SHOP.example.test")         # тот же origin: имя хоста без учёта регистра
    for origin in UNTRUSTED + ("*", "", "https://admin.example.test"):
        assert not trusted.allows(origin), origin


@pytest.mark.parametrize("value", [
    None, "", " , ", "*", "null", "http://shop.example.test", "https://*.example.test", f"{SHOP}/",
    f"{SHOP}/path", "https://user@shop.example.test", "shop.example.test",
    f"{SHOP}, http://localhost:5500", "https://пример.рф", f"{SHOP}:0",
])
def test_invalid_trusted_origins_refuse_start(value):
    with pytest.raises(AccountWebConfigError) as error:
        parse_trusted_origins(value)
    assert isinstance(error.value, AccountConfigError)
    with pytest.raises(AccountWebConfigError):
        load_account_web_config(ENABLED, {} if value is None else {"ACCOUNT_TRUSTED_ORIGINS": value,
                                                                   "CORS_ALLOWED_ORIGINS": ""}, _base([]), PROXIES)


# ---------- CORS в режиме аккаунтов ----------

def test_same_origin_deployment_needs_no_cors_but_trusts_own_origin():
    """Фронтенд и API на одном адресе: CORS пуст, свой адрес — только в ACCOUNT_TRUSTED_ORIGINS."""
    config = load_account_web_config(ENABLED, {"ACCOUNT_TRUSTED_ORIGINS": SHOP, "CORS_ALLOWED_ORIGINS": ""},
                                     _base([]), PROXIES)
    assert config.trusted_origins.allows(SHOP)
    assert config.cors_options["allow_origins"] == [] and config.cors_options["allow_credentials"] is True


def test_separate_frontend_gets_credentials_only_with_exact_list():
    base = _base([SHOP])
    options = account_cors_options(base, SHOP, parse_trusted_origins(SHOP))
    assert options["allow_origins"] == [SHOP] and options["allow_credentials"] is True
    assert "X-CSRF-Token" in options["allow_headers"] and "Content-Type" in options["allow_headers"]
    assert options["allow_methods"] == ["GET", "POST"] and "allow_origin_regex" not in options
    assert base["allow_credentials"] is False and base["allow_headers"] == ["Content-Type"]   # не изменён


def test_cors_list_must_be_explicit_in_accounts_mode():
    """Не задана — умолчания разработки cors_settings (http://localhost…) с credentials не применяются."""
    with pytest.raises(AccountWebConfigError):
        account_cors_options(_base(["http://localhost:5500"]), None, parse_trusted_origins(SHOP))


@pytest.mark.parametrize("cors_origins", [
    ["http://localhost:5500"], ["https://evil.example.test"], [SHOP, "https://evil.example.test"], ["*"],
    ["https://shop.example.test:8443"],
])
def test_every_cors_origin_must_be_trusted(cors_origins):
    with pytest.raises(AccountWebConfigError):
        account_cors_options(_base(cors_origins), ",".join(cors_origins), parse_trusted_origins(SHOP))


@pytest.mark.parametrize("override", [{"allow_origin_regex": r"https://.*\.example\.test"},
                                      {"allow_headers": ["*"]}, {"allow_methods": ["*"]}])
def test_wildcards_are_refused_with_credentials(override):
    with pytest.raises(AccountWebConfigError):
        account_cors_options({**_base([SHOP]), **override}, SHOP, parse_trusted_origins(SHOP))


def _cors_client(options):
    async def endpoint(request):
        return JSONResponse({"ok": True})
    app = Starlette(routes=[Route("/api/account/me", endpoint, methods=["GET", "POST"])],
                    middleware=[Middleware(CORSMiddleware, **options)])
    return TestClient(app)


def _preflight(client, origin, headers="content-type, x-csrf-token", method="POST"):
    return client.options("/api/account/me", headers={"Origin": origin, "Access-Control-Request-Method": method,
                                                      "Access-Control-Request-Headers": headers})


def test_real_cors_middleware_grants_credentials_only_to_the_exact_trusted_origin():
    config = load_account_web_config(ENABLED, {"ACCOUNT_TRUSTED_ORIGINS": f"{SHOP},{OTHER_TRUSTED}",
                                               "CORS_ALLOWED_ORIGINS": SHOP}, _base([SHOP]), PROXIES)
    client = _cors_client(config.cors_options)

    response = client.get("/api/account/me", headers={"Origin": SHOP, "Cookie": "__Host-ds_session=x"})
    assert response.headers["access-control-allow-origin"] == SHOP           # точный адрес, не '*'
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "origin" in response.headers["vary"].lower()
    preflight = _preflight(client, SHOP)
    assert preflight.status_code == 200 and preflight.headers["access-control-allow-origin"] == SHOP
    assert "x-csrf-token" in preflight.headers["access-control-allow-headers"].lower()

    # Произвольный сайт (и доверенный для CSRF, но не включённый в CORS) читать ответы не может;
    # CORSMiddleware сравнивает строку Origin точно, без отражения и шаблонов.
    for origin in UNTRUSTED + (OTHER_TRUSTED, "https://SHOP.example.test", SHOP + "/"):
        response = client.get("/api/account/me", headers={"Origin": origin, "Cookie": "__Host-ds_session=x"})
        assert "access-control-allow-origin" not in response.headers, origin
        preflight = _preflight(client, origin)
        assert preflight.status_code == 400 and "access-control-allow-origin" not in preflight.headers, origin
    assert _preflight(client, SHOP, headers="x-evil").status_code == 400
    assert _preflight(client, SHOP, method="DELETE").status_code == 400


def test_same_origin_config_gives_no_cors_access_to_anyone():
    config = load_account_web_config(ENABLED, {"ACCOUNT_TRUSTED_ORIGINS": SHOP, "CORS_ALLOWED_ORIGINS": ""},
                                     _base([]), PROXIES)
    client = _cors_client(config.cors_options)
    for origin in (SHOP,) + UNTRUSTED:
        assert "access-control-allow-origin" not in client.get("/api/account/me", headers={"Origin": origin}).headers


# ---------- лимиты и предупреждения ----------

def test_account_rate_rules_match_the_owner_decision():
    rules = {rule.name: rule for rule in ACCOUNT_RATE_RULES}
    assert {name: (rule.limit, rule.window_seconds, rule.per_client, rule.methods) for name, rule in rules.items()} == {
        "account_register": (1, 60, True, frozenset({"POST"})),
        "account_login": (10, 60, True, frozenset({"POST"})),
        "account_password": (5, 60, True, frozenset({"POST"})),
        "account_auth_per_process": (60, 60, False, frozenset({"POST"})),
    }
    per_process = rules["account_auth_per_process"].path
    assert all(per_process.match(p) for p in ("/api/account/login", "/api/account/register", "/api/account/login/"))
    assert not any(per_process.match(p) for p in ("/api/account/password", "/api/account/loginx", "/api/v2/orders"))
    assert rules["account_login"].path.match("/api/account/login") and not rules["account_login"].path.match(
        "/api/account/login/extra")


def test_body_limit_covers_only_account_paths():
    assert ACCOUNT_MAX_BODY_BYTES == 4096 and ACCOUNT_BODY_LIMITS == ((ACCOUNT_PATH, 4096),)
    assert all(ACCOUNT_PATH.match(p) for p in ("/api/account", "/api/account/", "/api/account/login",
                                               "/api/account/orders/5"))
    assert not any(ACCOUNT_PATH.match(p) for p in ("/api/accounts", "/api/accountx", "/api/v2/orders", "/orders"))


def test_startup_warnings_never_present_process_limits_as_global():
    with_proxy = account_startup_warnings({}, PROXIES)
    assert len(with_proxy) == 1
    assert "в каждом процессе отдельно" in with_proxy[0] and "limit_req" in with_proxy[0]
    assert any("TRUSTED_PROXY_IPS" in w for w in account_startup_warnings({}, ()))
    four = " ".join(account_startup_warnings({"WEB_CONCURRENCY": "4"}, PROXIES))
    assert "240/мин" in four and "4×" in four
    assert any("не распознано" in w for w in account_startup_warnings({"WEB_CONCURRENCY": "many"}, PROXIES))
    config = load_account_web_config(ENABLED, {"ACCOUNT_TRUSTED_ORIGINS": SHOP, "CORS_ALLOWED_ORIGINS": ""},
                                     _base([]), ())
    assert config.rate_rules == ACCOUNT_RATE_RULES and config.body_limits == ACCOUNT_BODY_LIMITS
    assert len(config.warnings) == 2


# ---------- без .env ----------

def test_account_web_modules_do_not_load_dotenv_or_database():
    """Импорт accounts.web, accounts.email и accounts.routes в чистом процессе не тянет database,
    cors_settings и dotenv."""
    code = (f"import sys; sys.path.insert(0, {str(PROJECT_DIR)!r}); "
            "import accounts.web, accounts.email, accounts.routes; "
            "print(sorted(m for m in ('database', 'cors_settings', 'dotenv') if m in sys.modules))")
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[]"
