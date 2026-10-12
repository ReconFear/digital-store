"""Настройки CORS для Digital Store.

CORS решает только одно: какие веб-страницы (origin = схема + хост + порт)
браузер пустит читать ответы нашего API из JavaScript.

CORS — НЕ защита API. Прямые HTTP-запросы (curl, Postman, скрипты, другой сервер)
CORS не ограничивает вообще: сервер обрабатывает запрос, браузер лишь не отдаёт
ответ странице с чужого origin. Защита данных — это аутентификация и проверка прав.

Список разрешённых origins задаётся переменной окружения CORS_ALLOWED_ORIGINS
(в .env или в окружении процесса) — через запятую:

    CORS_ALLOWED_ORIGINS=http://localhost:5500,http://127.0.0.1:5500

* Переменная не задана — используются локальные адреса разработки (DEFAULT_DEV_ORIGINS).
* Переменная задана пустой — браузерные запросы с других origins запрещены полностью
  (подходит, если frontend и API отдаются с одного адреса).
* "*", "null", адреса с путём или без схемы http/https — ошибка запуска: лучше упасть
  сразу, чем незаметно открыть API всем сайтам.
"""

import os
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

ENV_VAR = "CORS_ALLOWED_ORIGINS"

DEFAULT_DEV_ORIGINS = (
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)

# Методы, которые реально есть в API (GET /products, GET /products/{id},
# POST /orders, GET /orders). OPTIONS (preflight) обрабатывает сам middleware.
ALLOWED_METHODS = ("GET", "POST")

# Content-Type нужен для POST с JSON. Новые заголовки (например, Authorization)
# добавлять сюда только вместе с функцией, которой они нужны.
ALLOWED_HEADERS = ("Content-Type",)

# Сколько секунд браузер может кешировать ответ на preflight.
PREFLIGHT_MAX_AGE = 600


class CorsConfigError(ValueError):
    """Неверное значение CORS_ALLOWED_ORIGINS."""


def normalize_origin(raw):
    """Приводит origin к виду, который присылает браузер, или бросает CorsConfigError."""
    value = raw.strip()

    if value in ("*", "null"):
        raise CorsConfigError(
            f"{ENV_VAR}: значение {value!r} запрещено. Перечислите конкретные адреса, "
            "например http://localhost:5500"
        )

    # Частая ошибка — скопировать адрес с завершающим слешем.
    if value.endswith("/"):
        value = value[:-1]

    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise CorsConfigError(
            f"{ENV_VAR}: {raw.strip()!r} — не origin. Нужен формат схема://хост[:порт], "
            "например http://localhost:5500 или https://shop.example.com"
        )
    if parts.path or parts.query or parts.fragment or parts.username or parts.password:
        raise CorsConfigError(
            f"{ENV_VAR}: {raw.strip()!r} — origin не может содержать путь, параметры "
            "или логин. Только схема://хост[:порт]"
        )
    try:
        port = parts.port
    except ValueError as exc:
        raise CorsConfigError(f"{ENV_VAR}: {raw.strip()!r} — неверный порт") from exc

    host = parts.hostname.lower()
    if ":" in host:  # IPv6, например http://[::1]:5500
        host = f"[{host}]"
    origin = f"{parts.scheme}://{host}"
    if port is not None:
        origin += f":{port}"
    return origin


def get_cors_origins(environ=None):
    """Список разрешённых origins (без повторов, в исходном порядке)."""
    environ = os.environ if environ is None else environ
    raw = environ.get(ENV_VAR)

    if raw is None:
        return list(DEFAULT_DEV_ORIGINS)

    origins = []
    for item in raw.split(","):
        if not item.strip():
            continue
        origin = normalize_origin(item)
        if origin not in origins:
            origins.append(origin)
    return origins


def cors_middleware_options(environ=None):
    """Параметры для CORSMiddleware. Используется в api.py и в тестах."""
    return {
        "allow_origins": get_cors_origins(environ),
        "allow_credentials": False,
        "allow_methods": list(ALLOWED_METHODS),
        "allow_headers": list(ALLOWED_HEADERS),
        "max_age": PREFLIGHT_MAX_AGE,
    }
