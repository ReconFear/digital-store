"""Разбор CORS_ALLOWED_ORIGINS (cors_settings.py). Без FastAPI и без базы."""

import pytest

import cors_settings
from cors_settings import CorsConfigError, cors_middleware_options, get_cors_origins


def test_default_dev_origins_when_variable_is_not_set():
    assert get_cors_origins({}) == [
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def test_list_is_parsed_trimmed_normalized_and_deduplicated():
    env = {
        cors_settings.ENV_VAR: " http://localhost:5500 , HTTP://LocalHost:5500/,"
        "https://shop.example.com,,http://[::1]:5500"
    }

    assert get_cors_origins(env) == [
        "http://localhost:5500",
        "https://shop.example.com",
        "http://[::1]:5500",
    ]


def test_empty_variable_disables_cross_origin_access():
    assert get_cors_origins({cors_settings.ENV_VAR: ""}) == []
    assert get_cors_origins({cors_settings.ENV_VAR: " , "}) == []


@pytest.mark.parametrize(
    "bad",
    [
        "*",
        "null",
        "localhost:5500",
        "ftp://localhost",
        "http://localhost:5500/shop",
        "http://localhost:5500?x=1",
        "http://user:pass@localhost:5500",
        "http://localhost:99999",
        "http://",
    ],
)
def test_unsafe_or_invalid_origins_fail_fast(bad):
    with pytest.raises(CorsConfigError):
        get_cors_origins({cors_settings.ENV_VAR: f"http://localhost:5500,{bad}"})


def test_middleware_options_are_restrictive():
    options = cors_middleware_options({})

    assert options["allow_credentials"] is False
    assert options["allow_methods"] == ["GET", "POST"]
    assert options["allow_headers"] == ["Content-Type"]
    assert "*" not in options["allow_origins"]
    assert "*" not in options["allow_methods"]
    assert "*" not in options["allow_headers"]


def test_reads_process_environment(monkeypatch):
    monkeypatch.setenv(cors_settings.ENV_VAR, "https://shop.example.com")

    assert get_cors_origins() == ["https://shop.example.com"]
