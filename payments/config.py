"""Настройки платежей из переменных окружения.

По умолчанию платежи ВЫКЛЮЧЕНЫ (PAYMENTS_ENABLED не задан или false): маршруты оплаты
отвечают 503, обработчик уведомлений — 404, сверка ничего не делает.

Живые платежи требуют двух явных действий: YOOKASSA_MODE=live И
PAYMENTS_ALLOW_LIVE=I_UNDERSTAND_REAL_MONEY. Режим определяется и по ключу: у ЮKassa
тестовый секретный ключ начинается с "test_", боевой — с "live_"; несоответствие
режима и ключа — ошибка конфигурации.

Секретный ключ не попадает в repr/str настроек, в сообщения об ошибках и в логи.
"""

import ipaddress
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

LIVE_CONFIRMATION = "I_UNDERSTAND_REAL_MONEY"
DEFAULT_YOOKASSA_API_URL = "https://api.yookassa.ru/v3"

# Адреса, с которых ЮKassa отправляет уведомления (документация ЮKassa, раздел
# «Входящие уведомления»). Проверены 2026-10-13; перед запуском сверить с документацией.
YOOKASSA_NOTIFICATION_NETWORKS = (
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.156.11/32",
    "77.75.156.35/32",
    "77.75.154.128/25",
    "2a02:5180::/32",
)


# Домены, на которые разрешено отправлять покупателя по ссылке оплаты (confirmation_url).
# Ссылка из ответа провайдера выдаётся покупателю, только если её домен — один из этих
# или их поддомен. Проверено по примерам документации ЮKassa (yoomoney.ru); перед запуском
# сверить с тестовым магазином.
DEFAULT_CONFIRMATION_HOSTS = ("yoomoney.ru", "yookassa.ru")
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$")


class PaymentConfigError(ValueError):
    """Неверная конфигурация платежей. Сообщение никогда не содержит значения секрета."""


def _flag(env, name, default=False):
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    value = value.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise PaymentConfigError(f"{name}: ожидается true/false")


def is_loopback_host(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_url(name, value, allow_loopback_http):
    parts = urlsplit(value)
    if parts.username or parts.password:
        raise PaymentConfigError(f"{name}: учётные данные в адресе запрещены")
    if parts.scheme == "https" and parts.hostname:
        return value
    if allow_loopback_http and parts.scheme == "http" and parts.hostname and is_loopback_host(parts.hostname):
        return value
    raise PaymentConfigError(f"{name}: нужен адрес https:// (http допустим только для localhost)")


@dataclass(frozen=True)
class PaymentSettings:
    enabled: bool = False
    provider: str = "yookassa"
    shop_id: str = ""
    secret_key: str = field(default="", repr=False)
    mode: str = "test"
    api_url: str = DEFAULT_YOOKASSA_API_URL
    return_url: str = ""
    timeout_seconds: float = 10.0
    max_attempts: int = 3
    webhook_ip_check: bool = True
    trusted_proxies: tuple = ()
    notification_networks: tuple = YOOKASSA_NOTIFICATION_NETWORKS
    confirmation_hosts: tuple = DEFAULT_CONFIRMATION_HOSTS

    @property
    def expect_test_payments(self):
        return self.mode == "test"

    def __str__(self):
        return repr(self)


def load_payment_settings(env=None):
    env = os.environ if env is None else env
    enabled = _flag(env, "PAYMENTS_ENABLED", False)
    webhook_ip_check = _flag(env, "YOOKASSA_WEBHOOK_IP_CHECK", True)
    trusted = tuple(p.strip() for p in env.get("TRUSTED_PROXY_IPS", "").split(",") if p.strip())
    for proxy in trusted:
        try:
            ipaddress.ip_network(proxy, strict=False)
        except ValueError:
            raise PaymentConfigError("TRUSTED_PROXY_IPS: неверный адрес или сеть") from None
    if not enabled:
        return PaymentSettings(enabled=False, webhook_ip_check=webhook_ip_check, trusted_proxies=trusted)

    provider = env.get("PAYMENTS_PROVIDER", "yookassa").strip()
    if provider != "yookassa":
        raise PaymentConfigError("PAYMENTS_PROVIDER: поддерживается только yookassa")

    shop_id = env.get("YOOKASSA_SHOP_ID", "").strip()
    if not re.fullmatch(r"[0-9]{1,20}", shop_id):
        raise PaymentConfigError("YOOKASSA_SHOP_ID: нужен числовой идентификатор магазина")

    secret_key = env.get("YOOKASSA_SECRET_KEY", "").strip()
    if not secret_key:
        raise PaymentConfigError("YOOKASSA_SECRET_KEY не задан")
    if not re.fullmatch(r"[A-Za-z0-9_\-]{8,200}", secret_key):
        raise PaymentConfigError("YOOKASSA_SECRET_KEY: недопустимые символы или длина")

    mode = env.get("YOOKASSA_MODE", "test").strip().lower()
    if mode not in ("test", "live"):
        raise PaymentConfigError("YOOKASSA_MODE: test или live")
    if mode == "test" and not secret_key.startswith("test_"):
        raise PaymentConfigError("YOOKASSA_MODE=test, но ключ не тестовый (ожидается префикс test_)")
    if mode == "live":
        if env.get("PAYMENTS_ALLOW_LIVE", "") != LIVE_CONFIRMATION:
            raise PaymentConfigError(
                f"живые платежи выключены: нужен PAYMENTS_ALLOW_LIVE={LIVE_CONFIRMATION}")
        if not secret_key.startswith("live_"):
            raise PaymentConfigError("YOOKASSA_MODE=live, но ключ не боевой (ожидается префикс live_)")

    api_url = _check_url("YOOKASSA_API_URL", env.get("YOOKASSA_API_URL", DEFAULT_YOOKASSA_API_URL).strip().rstrip("/"),
                         allow_loopback_http=(mode == "test"))
    return_url = env.get("PAYMENT_RETURN_URL", "").strip()
    if not return_url:
        raise PaymentConfigError("PAYMENT_RETURN_URL не задан")
    return_url = _check_url("PAYMENT_RETURN_URL", return_url, allow_loopback_http=(mode == "test"))
    if len(return_url) > 2048:
        raise PaymentConfigError("PAYMENT_RETURN_URL слишком длинный")

    try:
        timeout = float(env.get("YOOKASSA_TIMEOUT_SECONDS", "10"))
        attempts = int(env.get("YOOKASSA_MAX_ATTEMPTS", "3"))
    except ValueError:
        raise PaymentConfigError("YOOKASSA_TIMEOUT_SECONDS / YOOKASSA_MAX_ATTEMPTS: нужны числа") from None
    if not 1 <= timeout <= 60 or not 1 <= attempts <= 5:
        raise PaymentConfigError("таймаут 1..60 секунд, попыток 1..5")

    hosts_raw = env.get("YOOKASSA_CONFIRMATION_HOSTS", "").strip()
    hosts = tuple(h.strip().lower() for h in hosts_raw.split(",") if h.strip()) if hosts_raw \
        else DEFAULT_CONFIRMATION_HOSTS
    if not all(HOST_RE.match(h) for h in hosts):
        raise PaymentConfigError("YOOKASSA_CONFIRMATION_HOSTS: список доменов через запятую")

    return PaymentSettings(
        enabled=True, provider=provider, shop_id=shop_id, secret_key=secret_key, mode=mode,
        api_url=api_url, return_url=return_url, timeout_seconds=timeout, max_attempts=attempts,
        webhook_ip_check=webhook_ip_check, trusted_proxies=trusted, confirmation_hosts=hosts,
    )
