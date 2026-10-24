"""Конфигурация HTTP-слоя аккаунтов (PR6 этап 2d): доверенные Origin, CORS, лимиты частоты и тела.

Чистые функции: без сети, базы и файлов. Окружение передаётся словарём; cors_settings.py и database.py не
импортируются (оба при импорте читают .env). Маршрутов здесь нет — подключение в api.py на этапе 2d-3.

Решения владельцев:
  * ACCOUNT_TRUSTED_ORIGINS — отдельный точный список https-origin страниц, которым разрешены изменяющие
    запросы аккаунта (проверка Origin/CSRF, accounts.request_guard). Собственный адрес магазина входит в него
    ЯВНО и при одном адресе фронтенда и API: браузер присылает Origin и на POST того же сайта;
  * CORS_ALLOWED_ORIGINS остаётся отдельным списком. В режиме аккаунтов CORS разрешает credentials, поэтому
    каждый его адрес обязан входить в ACCOUNT_TRUSTED_ORIGINS (иначе чужая страница читала бы ответы
    аккаунта, в том числе CSRF-токен из /api/account/me). Список задаётся явно: умолчания разработки
    cors_settings (http://localhost…) с credentials не применяются. Без '*', шаблонов и отражения Origin;
  * только https: cookie сессии — __Host-, Secure; послаблений для http://localhost нет;
  * лимиты: регистрация 1/мин на IP, вход 10/мин на IP, смена пароля 5/мин на IP, вход + регистрация
    60/мин на ПРОЦЕСС. Лимит процесса — не лимит установки: при N процессах до N×60/мин; общий предел
    обеспечивает обратный прокси (limit_req). Об этом — предупреждение при запуске. Эти правила применяют
    маршруты аккаунтов (accounts.routes) ПОСЛЕ проверки Origin, Content-Type, CSRF и входных данных —
    в RequestLimitsMiddleware их не добавляют: иначе межсайтовые и неверные запросы расходовали бы квоту;
  * тело запросов /api/account/* — не больше 4 КиБ (остальные маршруты — прежние лимиты).

При выключенных аккаунтах load_account_web_config окружение не читает и возвращает None: маршруты,
CORS и лимиты остаются как до PR6.
"""

import re
from dataclasses import dataclass

from accounts.config import AccountConfigError, AccountSettings
from accounts.request_guard import OriginAllowlist, OriginError, parse_origin
from http_security import RateRule

ENV_TRUSTED_ORIGINS = "ACCOUNT_TRUSTED_ORIGINS"
ENV_CORS_ORIGINS = "CORS_ALLOWED_ORIGINS"            # та же переменная, что в cors_settings.py
ENV_WORKERS = "WEB_CONCURRENCY"                      # число процессов uvicorn/gunicorn, если задано

ACCOUNT_PATH = re.compile(r"^/api/account(?:/|$)")
ACCOUNT_MAX_BODY_BYTES = 4 * 1024
ACCOUNT_BODY_LIMITS = ((ACCOUNT_PATH, ACCOUNT_MAX_BODY_BYTES),)
CSRF_HEADER_NAME = "X-CSRF-Token"

AUTH_PER_PROCESS_LIMIT = 60
ACCOUNT_RATE_RULES = (
    RateRule("account_register", frozenset({"POST"}), re.compile(r"^/api/account/register/?$"), 1),
    RateRule("account_login", frozenset({"POST"}), re.compile(r"^/api/account/login/?$"), 10),
    RateRule("account_password", frozenset({"POST"}), re.compile(r"^/api/account/password/?$"), 5),
    RateRule("account_auth_per_process", frozenset({"POST"}), re.compile(r"^/api/account/(?:login|register)/?$"),
             AUTH_PER_PROCESS_LIMIT, per_client=False),
)


class AccountWebConfigError(AccountConfigError):
    """Неверная настройка HTTP-слоя аккаунтов. Лучше не запуститься, чем открыть доступ."""


@dataclass(frozen=True)
class AccountWebConfig:
    trusted_origins: OriginAllowlist      # для accounts.request_guard (Origin/CSRF)
    cors_options: dict                    # параметры CORSMiddleware в режиме аккаунтов
    rate_rules: tuple                     # для accounts.routes (после защит), не для RequestLimitsMiddleware
    body_limits: tuple                    # path_body_limits для RequestLimitsMiddleware
    warnings: tuple                       # сообщения для журнала при запуске


def parse_trusted_origins(value):
    """ACCOUNT_TRUSTED_ORIGINS (через запятую) -> OriginAllowlist. Точные https-origin, хотя бы один."""
    origins = [item.strip() for item in (value or "").split(",") if item.strip()]
    if not origins:
        raise AccountWebConfigError(
            f"аккаунты включены, но {ENV_TRUSTED_ORIGINS} пуст: перечислите https-адреса страниц магазина, "
            "включая собственный адрес при одном адресе фронтенда и API")
    for position, origin in enumerate(origins, 1):
        try:
            scheme, _, _ = parse_origin(origin)
        except OriginError:
            raise AccountWebConfigError(
                f"{ENV_TRUSTED_ORIGINS}: запись №{position} — не origin вида https://хост[:порт] "
                "(без '*', 'null', пути, логина и завершающего '/'; хост в нижнем регистре, IDN — в punycode)"
            ) from None
        if scheme != "https":
            raise AccountWebConfigError(
                f"{ENV_TRUSTED_ORIGINS}: запись №{position} — только https (cookie сессии __Host-, Secure)")
    return OriginAllowlist(origins)


def account_cors_options(base_options, cors_env_value, trusted_origins):
    """Параметры CORSMiddleware в режиме аккаунтов из параметров cors_settings.cors_middleware_options().

    cors_env_value — значение CORS_ALLOWED_ORIGINS из окружения (None — не задана). Исходный словарь
    не изменяется."""
    if not isinstance(trusted_origins, OriginAllowlist):
        raise TypeError("trusted_origins: ожидается OriginAllowlist")
    if cors_env_value is None:
        raise AccountWebConfigError(
            f"в режиме аккаунтов {ENV_CORS_ORIGINS} задаётся явно: пустое значение — фронтенд и API на одном "
            f"адресе; иначе — точные адреса, каждый из которых есть в {ENV_TRUSTED_ORIGINS}")
    if base_options.get("allow_origin_regex"):
        raise AccountWebConfigError("CORS: шаблон origin (allow_origin_regex) с credentials запрещён")
    for name in ("allow_methods", "allow_headers"):
        if "*" in base_options.get(name, ()):
            raise AccountWebConfigError(f"CORS: '*' в {name} с credentials запрещён")
    origins = list(base_options.get("allow_origins", ()))
    for position, origin in enumerate(origins, 1):
        if origin == "*" or not trusted_origins.allows(origin):
            raise AccountWebConfigError(
                f"{ENV_CORS_ORIGINS}: запись №{position} не входит в {ENV_TRUSTED_ORIGINS} — с credentials "
                "CORS разрешён только доверенным https-адресам")
    headers = list(base_options.get("allow_headers", ()))
    if CSRF_HEADER_NAME.lower() not in {header.lower() for header in headers}:
        headers.append(CSRF_HEADER_NAME)
    return {**base_options, "allow_origins": origins, "allow_credentials": True, "allow_headers": headers}


def account_startup_warnings(env, trusted_proxies):
    """Предупреждения при запуске с включёнными аккаунтами (REV5 §4.1). Запуск они не останавливают."""
    warnings = []
    if not trusted_proxies:
        warnings.append(
            "TRUSTED_PROXY_IPS пуст: за обратным прокси все клиенты видны с адреса прокси, и лимиты аккаунтов "
            "по IP сработают для всех сразу. Укажите адреса прокси или подключайте приложение напрямую.")
    workers = (env.get(ENV_WORKERS) or "").strip()
    count = int(workers) if workers.isdigit() else None
    if workers and count is None:
        warnings.append(f"{ENV_WORKERS}: число процессов не распознано.")
    if count is not None and count > 1:
        scale = (f"при {ENV_WORKERS}={count} фактически до {count * AUTH_PER_PROCESS_LIMIT}/мин входов и "
                 f"регистраций на установку, по IP — до {count}× от лимита")
    else:
        scale = "при N процессах фактически до N× этих значений"
    warnings.append(
        f"Лимиты аккаунтов (вход и регистрация {AUTH_PER_PROCESS_LIMIT}/мин, лимиты по IP) действуют в каждом "
        f"процессе отдельно, а не на всю установку: {scale}. Общий предел задаётся на обратном прокси (limit_req).")
    return tuple(warnings)


def load_account_web_config(settings, env, base_cors_options, trusted_proxies):
    """AccountWebConfig при включённых аккаунтах; None — при выключенных (окружение не читается)."""
    if not isinstance(settings, AccountSettings):
        raise TypeError("settings: ожидается AccountSettings")
    if not settings.enabled:
        return None
    trusted = parse_trusted_origins(env.get(ENV_TRUSTED_ORIGINS))
    return AccountWebConfig(
        trusted_origins=trusted,
        cors_options=account_cors_options(base_cors_options, env.get(ENV_CORS_ORIGINS), trusted),
        rate_rules=ACCOUNT_RATE_RULES,
        body_limits=ACCOUNT_BODY_LIMITS,
        warnings=account_startup_warnings(env, trusted_proxies),
    )
