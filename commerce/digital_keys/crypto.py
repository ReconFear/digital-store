"""Шифрование цифровых ключей. Секреты — только из окружения процесса, в базу не попадают.

Настройки (значения — base64 от 32 случайных байт; создать: python -m commerce.digital_keys.admin generate-key):

  DIGITAL_KEYS_ENCRYPTION_KEYS  "k1:<base64>,k2:<base64>" — все версии, которыми зашифрованы ключи в базе
                                (веб-процессу для расшифровки, CLI импорта — для шифрования);
  DIGITAL_KEYS_ACTIVE_KEY       "k2" — версия для новых ключей (только CLI импорта);
  DIGITAL_KEYS_FINGERPRINT_KEY  "f1:<base64>" — секрет HMAC-отпечатков для поиска дубликатов
                                (только CLI импорта; веб-процессу и worker не нужен).

Формат записи (версия формата v1):
  шифротекст  = AES-256-GCM(ключ enc_key_id, nonce 12 байт случайно, открытый текст UTF-8, AAD)
  AAD         = b"digital-store/key/v1|" + product_id + "|" + public_id + "|" + enc_key_id
                (шифротекст нельзя переставить на другой товар, другую запись или другую версию ключа);
  отпечаток   = HMAC-SHA256(секрет отпечатков, b"digital-store/fp/v1|" + нормализованный ключ);
  проверочное значение версии (KCV) = первые 16 байт HMAC-SHA256(секрет,
                b"digital-store/kcv/v1/" + назначение + b"/" + key_id). По нему реестр в базе
                проверяет, что процессу дан тот же секрет, которым шифровали; сам секрет из KCV
                не восстановить.

Ни одно сообщение об ошибке не содержит значений секретов или ключей товара.
"""

import base64
import binascii
import hashlib
import hmac
import os
import re
import secrets
import uuid
from dataclasses import dataclass, field

KEY_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
KEY_BYTES = 32
NONCE_BYTES = 12
FORMAT_PREFIX = b"digital-store/key/v1|"
FINGERPRINT_PREFIX = b"digital-store/fp/v1|"
KCV_PREFIX = b"digital-store/kcv/v1/"
MAX_CODE_CHARS = 512

ENV_ENCRYPTION_KEYS = "DIGITAL_KEYS_ENCRYPTION_KEYS"
ENV_ACTIVE_KEY = "DIGITAL_KEYS_ACTIVE_KEY"
ENV_FINGERPRINT_KEY = "DIGITAL_KEYS_FINGERPRINT_KEY"
# Необязательно: путь к отдельному файлу секретов (формат .env) только для веб-процесса и CLI
# администратора. Так секреты не лежат в общем .env, который читает и worker.
ENV_SECRETS_FILE = "DIGITAL_KEYS_SECRETS_FILE"
SECRET_NAMES = (ENV_ENCRYPTION_KEYS, ENV_ACTIVE_KEY, ENV_FINGERPRINT_KEY)


def secret_settings(env=None):
    """Значения трёх настроек: из окружения, а если задан DIGITAL_KEYS_SECRETS_FILE — из этого файла
    (переменные окружения при этом имеют приоритет)."""
    env = os.environ if env is None else env
    values = {}
    path = (env.get(ENV_SECRETS_FILE) or "").strip()
    if path:
        try:
            from dotenv import dotenv_values
        except ImportError as exc:  # pragma: no cover — python-dotenv в requirements.txt
            raise KeyringError("нужен пакет python-dotenv") from exc
        if not os.path.isfile(path):
            raise KeyringError(f"{ENV_SECRETS_FILE}: файл не найден")
        loaded = dotenv_values(path)
        values.update({name: loaded[name] for name in SECRET_NAMES if loaded.get(name)})
    values.update({name: env[name] for name in SECRET_NAMES if env.get(name)})
    return values


class KeyringError(Exception):
    """Неверная или неполная настройка секретов. Сообщение без значений."""


class DecryptionError(Exception):
    """Шифротекст не расшифровывается (не тот ключ, повреждение, перестановка). Без подробностей."""


class InvalidCode(ValueError):
    """Строка не годится как ключ товара. Сообщение без значения."""


def _aesgcm():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover — зависимость указана в requirements.txt
        raise KeyringError("не установлен пакет cryptography (pip install -r requirements.txt)") from exc
    return AESGCM


def _decode_secret(name, key_id, text):
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise KeyringError(f"{name}: значение версии {key_id!r} — не base64") from None
    if len(raw) != KEY_BYTES:
        raise KeyringError(f"{name}: версия {key_id!r} должна быть 32 байта (сейчас {len(raw)})")
    return raw


def _parse_entries(name, value):
    entries = {}
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        key_id, sep, secret = part.partition(":")
        key_id = key_id.strip()
        if not sep or not KEY_ID_RE.match(key_id):
            raise KeyringError(f"{name}: ожидается формат id:base64 (id — a-z, 0-9, _ и -, до 32 символов)")
        if key_id in entries:
            raise KeyringError(f"{name}: версия {key_id!r} указана дважды")
        entries[key_id] = _decode_secret(name, key_id, secret)
    return entries


def check_value(secret, purpose, key_id):
    """Проверочное значение версии ключа (16 байт). Не секрет."""
    message = KCV_PREFIX + purpose.encode("ascii") + b"/" + key_id.encode("ascii")
    return hmac.new(secret, message, hashlib.sha256).digest()[:16]


@dataclass
class Keyring:
    """Секреты одного процесса. repr не показывает значения."""

    encryption: dict = field(default_factory=dict, repr=False)
    active: str = None
    fingerprint_id: str = None
    fingerprint_secret: bytes = field(default=None, repr=False)

    @classmethod
    def from_env(cls, env=None, *, need_active=False, need_fingerprint=False, need_encryption=True):
        env = secret_settings(env)
        encryption = _parse_entries(ENV_ENCRYPTION_KEYS, env.get(ENV_ENCRYPTION_KEYS, ""))
        if need_encryption and not encryption:
            raise KeyringError(f"не задан {ENV_ENCRYPTION_KEYS}")
        active = (env.get(ENV_ACTIVE_KEY) or "").strip() or None
        if need_active:
            if active is None:
                raise KeyringError(f"не задан {ENV_ACTIVE_KEY}")
            if active not in encryption:
                raise KeyringError(f"{ENV_ACTIVE_KEY}={active!r}: такой версии нет в {ENV_ENCRYPTION_KEYS}")
        fp_id = fp_secret = None
        fp_value = (env.get(ENV_FINGERPRINT_KEY) or "").strip()
        if fp_value:
            fp = _parse_entries(ENV_FINGERPRINT_KEY, fp_value)
            if len(fp) != 1:
                raise KeyringError(f"{ENV_FINGERPRINT_KEY}: нужна ровно одна версия")
            (fp_id, fp_secret), = fp.items()
        elif need_fingerprint:
            raise KeyringError(f"не задан {ENV_FINGERPRINT_KEY}")
        for key_id, secret in encryption.items():
            if fp_secret is not None and hmac.compare_digest(secret, fp_secret):
                raise KeyringError(f"секрет отпечатков совпадает с ключом шифрования {key_id!r}: нужны разные")
        return cls(encryption=encryption, active=active, fingerprint_id=fp_id, fingerprint_secret=fp_secret)

    # --- проверочные значения ---

    def check_values(self):
        """{"encryption:k1": hex, "fingerprint:f1": hex} для всех настроенных версий."""
        out = {f"encryption:{key_id}": check_value(secret, "encryption", key_id).hex()
               for key_id, secret in self.encryption.items()}
        if self.fingerprint_secret is not None:
            out[f"fingerprint:{self.fingerprint_id}"] = check_value(
                self.fingerprint_secret, "fingerprint", self.fingerprint_id).hex()
        return out

    def verify_against(self, registry, required):
        """Сверяет нужные версии с реестром базы ({"purpose:id": hex}). required — список "purpose:id".

        Нет в реестре -> KeyringError (зарегистрировать: verify-keyring --register).
        Другое значение -> KeyringError: процессу дан не тот секрет (расшифровка дала бы мусор/отказ).
        """
        mine = self.check_values()
        for name in required:
            if name not in mine:
                raise KeyringError(f"в окружении процесса нет версии {name}")
            registered = registry.get(name)
            if registered is None:
                raise KeyringError(f"версия {name} не зарегистрирована в базе (verify-keyring --register)")
            if not hmac.compare_digest(registered, mine[name]):
                raise KeyringError(f"версия {name}: секрет процесса НЕ совпадает с зарегистрированным")

    # --- шифрование ---

    def encrypt(self, plaintext, product_id, public_id, key_id=None):
        key_id = key_id or self.active
        if key_id not in self.encryption:
            raise KeyringError("нет активной версии ключа шифрования")
        nonce = secrets.token_bytes(NONCE_BYTES)
        ciphertext = _aesgcm()(self.encryption[key_id]).encrypt(
            nonce, plaintext.encode("utf-8"), aad(product_id, public_id, key_id))
        return key_id, nonce, ciphertext

    def decrypt(self, product_id, public_id, key_id, nonce, ciphertext):
        secret = self.encryption.get(key_id)
        if secret is None:
            raise DecryptionError("нет версии ключа шифрования")
        try:
            from cryptography.exceptions import InvalidTag
        except ImportError as exc:  # pragma: no cover
            raise DecryptionError("нет пакета cryptography") from exc
        try:
            plain = _aesgcm()(secret).decrypt(nonce, ciphertext, aad(product_id, public_id, key_id))
            return plain.decode("utf-8")
        except (InvalidTag, ValueError, UnicodeDecodeError):
            raise DecryptionError("шифротекст не прошёл проверку") from None

    def fingerprint(self, code):
        if self.fingerprint_secret is None:
            raise KeyringError(f"не задан {ENV_FINGERPRINT_KEY}")
        return hmac.new(self.fingerprint_secret, FINGERPRINT_PREFIX + code.encode("utf-8"), hashlib.sha256).digest()


def aad(product_id, public_id, key_id):
    if type(product_id) is not int or product_id <= 0:
        raise ValueError("product_id")
    public_id = str(uuid.UUID(str(public_id)))
    return FORMAT_PREFIX + f"{product_id}|{public_id}|{key_id}".encode("ascii")


_FORBIDDEN = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f  ​-‏‪-‮⁦-⁩﻿]")


def normalize_code(raw):
    """Ключ товара из строки файла. Регистр сохраняется (ключи бывают чувствительны к регистру).

    Убираются: BOM, концевые CR/LF, пробелы и табуляции по краям. Внутри строки управляющие и
    невидимые символы (кроме табуляции) запрещены. Пустая строка -> None (пропускается).
    """
    if not isinstance(raw, str):
        raise InvalidCode("ключ должен быть строкой")
    code = raw.replace("﻿", "", 1) if raw.startswith("﻿") else raw
    code = code.rstrip("\r\n").strip(" \t")
    if not code:
        return None
    if _FORBIDDEN.search(code):
        raise InvalidCode("в ключе управляющие или невидимые символы")
    if len(code) > MAX_CODE_CHARS:
        raise InvalidCode(f"ключ длиннее {MAX_CODE_CHARS} символов")
    return code


def generate_secret_b64():
    return base64.b64encode(secrets.token_bytes(KEY_BYTES)).decode("ascii")
