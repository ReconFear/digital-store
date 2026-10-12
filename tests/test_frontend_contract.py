"""Статические проверки frontend-моста. Не требуют FastAPI, браузера и базы данных.

Проверяют, что JS-клиент не ссылается на несуществующие эндпоинты, что демо
не вставляет данные через innerHTML и не создаёт заказы, и что настройки
портов в документации, CORS и демо согласованы.
"""

import re
from pathlib import Path

import cors_settings

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
CLIENT = FRONTEND / "lib" / "digital-store-api.js"
DEMO = FRONTEND / "examples" / "vanilla-storefront"


def _read(path):
    return path.read_text(encoding="utf-8-sig")


def _normalize(path):
    return re.sub(r"\{[^}]+\}", "{}", path)


def _api_routes():
    source = _read(ROOT / "api.py")
    return {_normalize(p) for p in re.findall(r'@app\.(?:get|post|put|patch|delete)\(\s*"([^"]+)"', source)}


def _client_endpoints():
    source = _read(CLIENT)
    block = re.search(r"export const ENDPOINTS = Object\.freeze\(\{(.*?)\}\);", source, re.S)
    assert block, "в клиенте не найден ENDPOINTS"
    return {_normalize(p) for p in re.findall(r'"(/[^"]*)"', block.group(1))}


def _frontend_scripts():
    return sorted(FRONTEND.rglob("*.js")) + sorted(FRONTEND.rglob("*.mjs"))


def test_client_uses_only_existing_api_routes():
    endpoints = _client_endpoints()

    assert endpoints == {"/products", "/products/{}", "/orders"}
    assert endpoints <= _api_routes()


def test_client_only_uses_paths_from_endpoints():
    source = _read(CLIENT)
    literal_paths = set(re.findall(r'["`](/(?:products|orders)[^"`?]*)', source))

    assert {_normalize(p) for p in literal_paths} <= _client_endpoints()


def test_frontend_never_injects_html_or_evaluates_code():
    forbidden = re.compile(r"\binnerHTML\b|\bouterHTML\b|insertAdjacentHTML|document\.write|\beval\(|new Function\(")
    offenders = [str(p.relative_to(ROOT)) for p in _frontend_scripts() if forbidden.search(_read(p))]

    assert offenders == []


def test_frontend_pages_have_no_inline_scripts():
    for page in FRONTEND.rglob("*.html"):
        html = _read(page)
        assert re.search(r"<script(?![^>]*\bsrc=)[^>]*>", html) is None, page
        assert re.search(r"\son[a-z]+\s*=", html) is None, page


def test_demo_storefront_does_not_create_orders():
    app = _read(DEMO / "app.js")

    assert "createOrder" not in app
    assert "allowOrderCreation" not in app


def test_order_creation_is_opt_in_in_client():
    source = _read(CLIENT)

    assert "options.allowOrderCreation === true" in source
    assert 'credentials: "omit"' in source


def test_no_secrets_or_backend_internals_in_frontend():
    for path in FRONTEND.rglob("*"):
        if path.is_file() and path.suffix in {".js", ".mjs", ".html", ".css"}:
            text = _read(path)
            for marker in ("DB_PASSWORD", "DB_USER", "postgresql://", "postgres://"):
                assert marker not in text, f"{path}: {marker}"


def test_ports_are_consistent_between_demo_server_cors_and_config():
    serve = _read(FRONTEND / "serve.py")
    default_port = re.search(r'"--port", type=int, default=(\d+)', serve).group(1)

    assert f"http://127.0.0.1:{default_port}" in cors_settings.DEFAULT_DEV_ORIGINS
    assert 'API_BASE_URL = "http://127.0.0.1:8000"' in _read(DEMO / "config.js")


def test_demo_server_binds_to_localhost_only():
    serve = _read(FRONTEND / "serve.py")

    assert '("127.0.0.1", args.port)' in serve
    assert "directory=str(FRONTEND_DIR)" in serve
