"""PR6: доказательство пароля (PR6_DESIGN_REV5 §5.1, тесты P1, P2). Только офлайн — без базы и сети.

Перец в тестах синтетический (bytes(range(32))), эталонные значения вычислены независимо от accounts.proof
(hmac из стандартной библиотеки) — тест ловит любое изменение формата: префикс, NFC, кодировку, base64url.
"""

import re
import unicodedata

import pytest

from accounts.config import AccountSettings, Pepper
from accounts.proof import (
    MESSAGE_PREFIX, PROOF_RE, PasswordPolicyError, PasswordProofs, normalize_password, password_proof,
    password_proofs,
)

PEPPER_1 = Pepper(1, bytes(range(32)))
PEPPER_2 = Pepper(2, bytes(range(32, 64)))
# Эталонные векторы (перец PEPPER_1).
KNOWN_ANSWERS = {
    "correct horse battery staple": "ZaNfHUEAZ76EHjorfWV85uhubqlHPuVyBy_lR4qeZy0",
    "Пароль с ёлкой é 2026": "JxLgPtFdlXigX4dtC4yemnQ5SUxGwuIjN5qKJKbPTcE",
}
# Тот же формат, что проверяет база: auth_check_proof в migrations/0005_customer_auth.sql.
SQL_PROOF_PATTERN = "^[A-Za-z0-9_-]{43}$"


@pytest.mark.parametrize("password, expected", KNOWN_ANSWERS.items())
def test_known_answer_vectors(password, expected):
    assert password_proof(PEPPER_1, password) == expected


def test_nfc_and_nfd_forms_give_the_same_proof():
    """P1: NFC и NFD одного пароля — одно доказательство."""
    nfc = "Пароль с ёлкой é 2026"
    nfd = unicodedata.normalize("NFD", nfc)
    assert nfd != nfc
    assert password_proof(PEPPER_1, nfd) == password_proof(PEPPER_1, nfc) == KNOWN_ANSWERS[nfc]


def test_proof_format_matches_the_database_check():
    proof = password_proof(PEPPER_1, "correct horse battery staple")
    assert len(proof) == 43 and "=" not in proof
    assert PROOF_RE.pattern == SQL_PROOF_PATTERN and re.fullmatch(SQL_PROOF_PATTERN, proof)
    # Перед паролем — префикс с U+0000 (в пароле U+0000 невозможен, граница однозначна).
    assert MESSAGE_PREFIX == b"digital-store/pw/v1\x00"


def test_different_pepper_or_password_gives_different_proof():
    password = "correct horse battery staple"
    assert password_proof(PEPPER_1, password) != password_proof(PEPPER_2, password)
    assert password_proof(PEPPER_1, password) != password_proof(PEPPER_1, password + "!")
    assert password_proof(PEPPER_1, " " + password) != password_proof(PEPPER_1, password)   # пробелы сохраняются


@pytest.mark.parametrize("password, code", [
    ("a" * 9, "password_too_short"),
    ("a" * 129, "password_too_long"),
    ("a" * 10, None),                                         # граница: 10 символов
    ("я" * 128, None),                                        # 128 символов, 256 байт
    ("😀" * 128, None),                                        # 128 символов, ровно 512 байт
    ("a" * 9 + "\x00", "password_forbidden_character"),
    ("abcdefghij\x00", "password_forbidden_character"),
    ("abcdefghij\n", "password_forbidden_character"),
    ("abcdefghij\t", "password_forbidden_character"),
    ("abcdefghij\x7f", "password_forbidden_character"),
    ("abcdefghij\x85", "password_forbidden_character"),        # C1 (NEL) — тоже категория Cc
    ("abcdefghij\ud800", "password_forbidden_character"),      # одиночный суррогат (Cs): не кодируется в UTF-8
    ("\udfffabcdefghij", "password_forbidden_character"),
])
def test_password_policy(password, code):
    """P2: 9 и 129 символов, U+0000 и управляющие символы — отказ до обращения к базе."""
    if code is None:
        assert normalize_password(password) == unicodedata.normalize("NFC", password)
        return
    with pytest.raises(PasswordPolicyError) as error:
        normalize_password(password)
    assert error.value.code == code


def test_513_bytes_are_refused():
    """P2: 513 байт UTF-8 — отказ. Символ UTF-8 занимает не больше 4 байт, поэтому 512 байт — предел для
    128 символов, а 513 байт возможны только при 129+ символах: такой пароль отклоняется как слишком
    длинный (проверка байтов остаётся второй линией по REV5 §5.1)."""
    password = "😀" * 128 + "a"
    assert len(password.encode("utf-8")) == 513
    with pytest.raises(PasswordPolicyError) as error:
        normalize_password(password)
    assert error.value.code == "password_too_long"
    assert len(("😀" * 128).encode("utf-8")) == 512
    normalize_password("😀" * 128)


@pytest.mark.parametrize("value", [None, b"bytes password", 12345678901])
def test_non_string_password_is_refused(value):
    with pytest.raises(PasswordPolicyError) as error:
        normalize_password(value)
    assert error.value.code == "password_invalid"


def test_errors_and_reprs_do_not_contain_secrets():
    # Суррогат: UnicodeEncodeError хранил бы весь пароль в args (и в repr) — должен быть PasswordPolicyError.
    for secret_password in ("Секретный пароль 2026\n", "Секретный пароль 2026\ud800"):
        with pytest.raises(PasswordPolicyError) as error:
            password_proof(PEPPER_1, secret_password)
        assert "Секретный" not in str(error.value) and "Секретный" not in repr(error.value)
        assert error.value.__cause__ is None and error.value.__context__ is None
    proofs = password_proofs(AccountSettings(enabled=True, pepper_current=PEPPER_2, pepper_previous=PEPPER_1,
                                             csrf_key=bytes(range(64, 96))), "correct horse battery staple")
    text = repr(proofs) + repr(PEPPER_1)
    assert proofs.current not in text and proofs.previous not in text
    assert repr(bytes(range(32))) not in text


def test_proofs_for_current_and_previous_pepper():
    settings = AccountSettings(enabled=True, pepper_current=PEPPER_2, pepper_previous=PEPPER_1,
                               csrf_key=bytes(range(64, 96)))
    proofs = password_proofs(settings, "correct horse battery staple")
    assert isinstance(proofs, PasswordProofs)
    assert (proofs.current_version, proofs.previous_version) == (2, 1)
    assert proofs.current == password_proof(PEPPER_2, "correct horse battery staple")
    assert proofs.previous == KNOWN_ANSWERS["correct horse battery staple"]

    only_current = password_proofs(AccountSettings(enabled=True, pepper_current=PEPPER_1,
                                                   csrf_key=bytes(range(64, 96))), "correct horse battery staple")
    assert (only_current.previous, only_current.previous_version) == (None, None)


def test_proofs_unavailable_when_accounts_are_disabled():
    with pytest.raises(RuntimeError):
        password_proofs(AccountSettings(enabled=False), "correct horse battery staple")
    with pytest.raises(TypeError):
        password_proof(bytes(range(32)), "correct horse battery staple")
