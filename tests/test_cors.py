"""CORS настоящего приложения api.app (FastAPI TestClient, без базы данных).

Разрешённый origin берётся из той же конфигурации, что и у приложения,
поэтому тесты не ломаются, если в .env задан свой CORS_ALLOWED_ORIGINS.
"""

import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

import api
from cors_settings import cors_middleware_options

FOREIGN_ORIGIN = "https://evil.example"


def _app_cors_options():
    for middleware in api.app.user_middleware:
        if middleware.cls is CORSMiddleware:
            return middleware.kwargs
    raise AssertionError("CORSMiddleware не подключён к api.app")


@pytest.fixture
def allowed_origin():
    origins = _app_cors_options()["allow_origins"]
    if not origins:
        pytest.skip("CORS_ALLOWED_ORIGINS задан пустым — разрешённых origins нет")
    return origins[0]


@pytest.fixture
def client(monkeypatch):
    product = {"id": 1, "name": "Test", "price": 100, "quantity": 2, "is_for_sale": True}
    monkeypatch.setattr(api, "get_public_products_from_db", lambda available_only=False: [product])
    monkeypatch.setattr(api, "get_public_product_from_db", lambda product_id: None)
    return TestClient(api.app)


def test_app_uses_cors_settings_module():
    options = _app_cors_options()

    assert options == cors_middleware_options()
    assert options["allow_credentials"] is False
    assert "*" not in options["allow_origins"]
    assert FOREIGN_ORIGIN not in options["allow_origins"]


def test_allowed_origin_gets_cors_headers(client, allowed_origin):
    response = client.get("/products", headers={"Origin": allowed_origin})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == allowed_origin
    assert "access-control-allow-credentials" not in response.headers
    assert "origin" in response.headers.get("vary", "").lower()


def test_foreign_origin_gets_no_cors_headers(client):
    response = client.get("/products", headers={"Origin": FOREIGN_ORIGIN})

    # CORS не блокирует запрос на сервере: ответ сформирован (200),
    # но без заголовка браузер не отдаст его странице чужого сайта.
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_request_without_origin_is_unaffected(client):
    response = client.get("/products")

    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert "access-control-allow-origin" not in response.headers


def test_preflight_for_order_creation_from_allowed_origin(client, allowed_origin):
    response = client.options(
        "/orders",
        headers={
            "Origin": allowed_origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == allowed_origin
    assert "POST" in response.headers["access-control-allow-methods"]
    assert "content-type" in response.headers["access-control-allow-headers"].lower()
    assert "access-control-allow-credentials" not in response.headers


def test_preflight_from_foreign_origin_is_rejected(client):
    response = client.options(
        "/orders",
        headers={
            "Origin": FOREIGN_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("method", ["DELETE", "PUT", "PATCH"])
def test_preflight_with_unsupported_method_is_rejected(client, allowed_origin, method):
    response = client.options(
        "/orders",
        headers={"Origin": allowed_origin, "Access-Control-Request-Method": method},
    )

    assert response.status_code == 400


def test_preflight_with_unexpected_header_is_rejected(client, allowed_origin):
    response = client.options(
        "/products",
        headers={
            "Origin": allowed_origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )

    assert response.status_code == 400


def test_null_origin_is_not_allowed(client):
    # Страница, открытая как file://, присылает Origin: null.
    response = client.get("/products", headers={"Origin": "null"})

    assert "access-control-allow-origin" not in response.headers


def test_api_contract_unchanged_with_origin(client, allowed_origin):
    response = client.get("/products", headers={"Origin": allowed_origin})

    assert set(response.json()) == {"products", "total"}
    assert client.get("/products/5", headers={"Origin": allowed_origin}).status_code == 404
    assert client.get("/orders", headers={"Origin": allowed_origin}).status_code == 403
