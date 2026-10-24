"""Настройки аккаунтов покупателей (PR6). Ничего не подключает и не пишет.

Переменные окружения процесса:

  DS_ACCOUNTS_ENABLED      on/off (по умолчанию off). Раздел аккаунтов работает, только если включён И здесь,
                           И в базе (auth_settings.accounts_enabled) — PR6_DESIGN_REV5_ADDENDUM2 §2.
  DS_ACCOUNT_SECRETS_FILE  путь к файлу секретов аккаунтов (формат .env). Нужен только при включённых
                           аккаунтах; при выключенных файл не читается вовсе.

Файл секретов (отдельно от .env с учётными данными базы — PR6_DESIGN_REV4 §1.3, решение Б2):

  ACCOUNT_PEPPER_CURRENT   "<версия>:<base64url от 32 случайных байт>" — перец текущей версии;
  ACCOUNT_PEPPER_PREVIOUS  необязательно, тот же формат — предыдущая версия на период смены перца;
                           её номер меньше текущего (так требует и база: ver_prev < ver_cur);
  ACCOUNT_CSRF_KEY         "<base64url от 32 случайных байт>" — ключ CSRF-токенов (REV4 §6.2).

Версия перца — целое 1…32767. Все три секрета различны. Других имён в файле быть не должно (опечатка
не должна молча оставить перец без предыдущей версии). Эти имена в окружении процесса запрещены: секреты
не должны попадать в .env, который читают и другие процессы.

Ни одно сообщение об ошибке и ни один repr не содержит значений секретов.
"""

import base64
import binascii
import hmac
import os
import re
from dataclasses import dataclass, field

ENV_ENABLED = "DS_ACCOUNTS_ENABLED"
ENV_SECRETS_FILE = "DS_ACCOUNT_SECRETS_FILE"
PEPPER_CURRENT = "ACCOUNT_PEPPER_CURRENT"
PEPPER_PREVIOUS = "ACCOUNT_PEPPER_PREVIOUS"
CSRF_KEY = "ACCOUNT_CSRF_KEY"
SECRET_NAMES = (PEPPER_CURRENT, PEPPER_PREVIOUS, CSRF_KEY)

SECRET_BYTES = 32
MIN_PEPPER_VERSION = 1
MAX_PEPPER_VERSION = 32767
_B64URL_32 = re.compile(r"^[A-Za-z0-9_-]{43}=?$")
_VERSION = re.compile(r"^[0-9]{1,5}$")


class AccountConfigError(Exception):
    """Неверная или неполная настройка аккаунтов. Сообщение без значений секретов."""


@dataclass(frozen=True)
class Pepper:
    """Перец одной версии. repr показывает только номер версии."""

    version: int
    secret: bytes = field(repr=False)


@dataclass(frozen=True)
class AccountSettings:
    enabled: bool = False
    pepper_current: Pepper = None
    pepper_previous: Pepper = None
    csrf_key: bytes = field(default=None, repr=False)


def parse_enabled(value):
    """on/true/1/yes -> True; off/false/0/no/пусто -> False; иначе ошибка (опечатка не включает аккаунты)."""
    text = (value or "").strip().lower()
    if text in ("on", "true", "1", "yes"):
        return True
    if text in ("", "off", "false", "0", "no"):
        return False
    raise AccountConfigError(f"{ENV_ENABLED}: ожидается on или off")


def decode_secret(name, text):
    """base64url (с '=' в конце или без) от ровно 32 байт."""
    text = (text or "").strip()
    if not _B64URL_32.match(text):
        raise AccountConfigError(f"{name}: ожидается base64url от {SECRET_BYTES} случайных байт")
    try:
        raw = base64.urlsafe_b64decode(text.rstrip("=") + "=")
    except (binascii.Error, ValueError):
        raise AccountConfigError(f"{name}: значение не декодируется как base64url") from None
    if len(raw) != SECRET_BYTES:
        raise AccountConfigError(f"{name}: нужно {SECRET_BYTES} байт")
    return raw


def parse_pepper(name, text):
    """"<версия>:<base64url>" -> Pepper."""
    version, sep, secret = (text or "").strip().partition(":")
    if not sep or not _VERSION.match(version.strip()):
        raise AccountConfigError(f"{name}: ожидается формат <версия>:<base64url>")
    number = int(version)
    if not MIN_PEPPER_VERSION <= number <= MAX_PEPPER_VERSION:
        raise AccountConfigError(f"{name}: версия должна быть от {MIN_PEPPER_VERSION} до {MAX_PEPPER_VERSION}")
    return Pepper(number, decode_secret(name, secret))


def _read_secrets_file(path):
    try:
        from dotenv import dotenv_values
    except ImportError as exc:  # pragma: no cover — python-dotenv в requirements.txt
        raise AccountConfigError("нужен пакет python-dotenv") from exc
    if not os.path.isfile(path):
        raise AccountConfigError(f"{ENV_SECRETS_FILE}: файл не найден")
    values = dotenv_values(path)
    unknown = sorted(set(values) - set(SECRET_NAMES))
    if unknown:
        raise AccountConfigError(f"{ENV_SECRETS_FILE}: неизвестные имена {unknown} (допустимы {list(SECRET_NAMES)})")
    return values


def load_account_settings(env=None):
    """Настройки из окружения процесса. При выключенных аккаунтах файл секретов не читается."""
    env = os.environ if env is None else env
    leaked = sorted(name for name in SECRET_NAMES if env.get(name))
    if leaked:
        raise AccountConfigError(f"секреты аккаунтов {leaked} заданы в окружении процесса: храните их только "
                                 f"в файле {ENV_SECRETS_FILE}")
    if not parse_enabled(env.get(ENV_ENABLED)):
        return AccountSettings(enabled=False)

    path = (env.get(ENV_SECRETS_FILE) or "").strip()
    if not path:
        raise AccountConfigError(f"аккаунты включены ({ENV_ENABLED}), но не задан {ENV_SECRETS_FILE}")
    values = _read_secrets_file(path)
    if not values.get(PEPPER_CURRENT):
        raise AccountConfigError(f"{ENV_SECRETS_FILE}: не задан {PEPPER_CURRENT}")
    if not values.get(CSRF_KEY):
        raise AccountConfigError(f"{ENV_SECRETS_FILE}: не задан {CSRF_KEY}")
    current = parse_pepper(PEPPER_CURRENT, values[PEPPER_CURRENT])
    previous = parse_pepper(PEPPER_PREVIOUS, values[PEPPER_PREVIOUS]) if values.get(PEPPER_PREVIOUS) else None
    csrf_key = decode_secret(CSRF_KEY, values[CSRF_KEY])

    if previous is not None and previous.version >= current.version:
        raise AccountConfigError(f"{PEPPER_PREVIOUS}: номер версии должен быть меньше, чем у {PEPPER_CURRENT}")
    secrets_in_use = [current.secret, csrf_key] + ([previous.secret] if previous else [])
    for i, first in enumerate(secrets_in_use):
        for second in secrets_in_use[i + 1:]:
            if hmac.compare_digest(first, second):
                raise AccountConfigError(f"{ENV_SECRETS_FILE}: секреты должны быть разными")
    return AccountSettings(enabled=True, pepper_current=current, pepper_previous=previous, csrf_key=csrf_key)
