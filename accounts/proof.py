"""Доказательство пароля (PR6_DESIGN_REV5 §5.1). Пароль в базу не передаётся никогда.

  пароль         строка; нормализация NFC; 10–128 символов после NFC и не больше 512 байт UTF-8;
                 запрещены U+0000, управляющие символы (категория Cc) и одиночные суррогаты (Cs: не
                 кодируются в UTF-8); пробелы сохраняются;
  сообщение      b"digital-store/pw/v1\\x00" + NFC(пароль).encode("utf-8")
                 (U+0000 в пароле невозможен, поэтому граница префикса однозначна);
  доказательство base64url(HMAC-SHA256(перец версии v, сообщение)) без '=' — ровно 43 символа
                 [A-Za-z0-9_-]; этот же формат проверяет база (auth_check_proof, миграция 0005).

База хэширует доказательство bcrypt (ext_pgcrypto.crypt). Без перца, который есть только у веб-процесса,
доказательство не построить — SQL-доступ под ролью app не даёт подбора паролей (REV4 §1).

Ни одно исключение и ни один repr не содержит пароля или доказательства.
"""

import base64
import hashlib
import hmac
import re
import unicodedata
from dataclasses import dataclass, field

from accounts.config import AccountSettings, Pepper

MESSAGE_PREFIX = b"digital-store/pw/v1\x00"
MIN_PASSWORD_CHARS = 10
MAX_PASSWORD_CHARS = 128
MAX_PASSWORD_BYTES = 512
PROOF_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


class PasswordPolicyError(ValueError):
    """Пароль не подходит под правила. code — машинный код для ответа 422; пароля в сообщении нет."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PasswordProofs:
    """Доказательства для вызова функций базы. repr показывает только номера версий.

    current/current_version — текущая версия перца; previous/previous_version — предыдущая
    (None, если смены перца нет). Для регистрации и НОВОГО пароля при смене используется только current."""

    current: str = field(repr=False)
    current_version: int
    previous: str = field(default=None, repr=False)
    previous_version: int = None


def normalize_password(password):
    """NFC-форма пароля после проверки правил REV5 §5.1."""
    if not isinstance(password, str):
        raise PasswordPolicyError("password_invalid", "пароль должен быть строкой")
    normalized = unicodedata.normalize("NFC", password)
    # Суррогат — до encode("utf-8") ниже: иначе UnicodeEncodeError, в repr которого весь пароль.
    if any(ch == "\x00" or unicodedata.category(ch) in ("Cc", "Cs") for ch in normalized):
        raise PasswordPolicyError("password_forbidden_character", "пароль содержит недопустимый символ")
    if len(normalized) < MIN_PASSWORD_CHARS:
        raise PasswordPolicyError("password_too_short",
                                  f"пароль должен быть не короче {MIN_PASSWORD_CHARS} символов")
    if len(normalized) > MAX_PASSWORD_CHARS:
        raise PasswordPolicyError("password_too_long",
                                  f"пароль должен быть не длиннее {MAX_PASSWORD_CHARS} символов")
    if len(normalized.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError("password_too_long", f"пароль длиннее {MAX_PASSWORD_BYTES} байт")
    return normalized


def password_proof(pepper, password):
    """Доказательство пароля для перца одной версии: 43 символа base64url."""
    if not isinstance(pepper, Pepper):
        raise TypeError("pepper: ожидается accounts.config.Pepper")
    message = MESSAGE_PREFIX + normalize_password(password).encode("utf-8")
    digest = hmac.new(pepper.secret, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def password_proofs(settings, password):
    """Доказательства для текущей и (если настроена) предыдущей версии перца."""
    if not isinstance(settings, AccountSettings) or not settings.enabled or settings.pepper_current is None:
        raise RuntimeError("аккаунты выключены или не настроены: доказательство пароля недоступно")
    current = settings.pepper_current
    previous = settings.pepper_previous
    return PasswordProofs(
        current=password_proof(current, password),
        current_version=current.version,
        previous=password_proof(previous, password) if previous is not None else None,
        previous_version=previous.version if previous is not None else None,
    )
