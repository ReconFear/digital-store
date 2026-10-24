"""PR6 этап 2d-1: email на HTTP-входе (accounts/email.py) — только ASCII. Офлайн, без базы и .env."""

import pytest

from accounts.email import MAX_RAW_EMAIL_CHARS, normalize_email
from accounts.service import AccountInputError, check_email_norm

LONGEST = "x" * 241 + "@example.test"                  # 254 символа — предел контракта базы


@pytest.mark.parametrize("raw, expected", [
    ("buyer@example.test", "buyer@example.test"),
    ("Buyer@Example.TEST", "buyer@example.test"),
    ("  buyer@example.test\t\r\n\f\v", "buyer@example.test"),        # ASCII-пробелы по краям обрезаются
    ("A.B+Tag@Sub.Example.test", "a.b+tag@sub.example.test"),      # точки и +tag не меняются
    ("o'neil!#$%&*/=?^_`{|}~-@example.test", "o'neil!#$%&*/=?^_`{|}~-@example.test"),
    (LONGEST.upper(), LONGEST),
])
def test_valid_ascii_addresses(raw, expected):
    assert normalize_email(raw) == expected
    assert normalize_email(expected) == expected                    # нормализация идемпотентна
    assert check_email_norm(expected) == expected                   # SQL-совместимый контракт


@pytest.mark.parametrize("raw", [
    "", "   ", "buyer", "buyer@", "@example.test", "a@b@example.test",
    "a b@example.test", "buyer@exam ple.test", "buyer@example.test\nx", "a\tb@example.test",
    "a\x00b@example.test", "a\x01b@example.test", "a\x7fb@example.test",
    # Unicode в PR6 не поддерживается — в том числе символы, которые lower() превратил бы в ASCII.
    "покупатель@пример.рф", "buyer@пример.рф", "bü@example.test",
    "K@example.test",                       # KELVIN SIGN: lower() -> «k»
    "İ@example.test",                       # İ: lower() -> «i̇»
    "buyer＠example.test",                   # полноширинная «@»
    "buyer@example.test ",                  # NBSP по краю — не ASCII-пробел
    "​buyer@example.test",                  # ZERO WIDTH SPACE
    "buyer@example.test\ud800",                  # одиночный суррогат
    "x" * 242 + "@example.test",                 # 255 символов
    " " * (MAX_RAW_EMAIL_CHARS - 2) + "a@b",     # длиннее предела до обрезки
    None, 42, b"buyer@example.test", ["buyer@example.test"],
])
def test_invalid_addresses_are_refused(raw):
    with pytest.raises(AccountInputError) as error:
        normalize_email(raw)
    assert error.value.code == "email_invalid"


@pytest.mark.parametrize("raw", ["released+1@invalid", " Buyer@Shop.INVALID "])
def test_reserved_domain_is_refused(raw):
    with pytest.raises(AccountInputError) as error:
        normalize_email(raw)
    assert error.value.code == "email_reserved"


@pytest.mark.parametrize("raw", [
    "Secret-Marker x@example.test", "Secret-Marker@example.invalid", "Secret-Markeré@example.test",
    "Secret-Marker" * 30,
])
def test_errors_do_not_contain_the_input(raw):
    with pytest.raises(AccountInputError) as error:
        normalize_email(raw)
    text = f"{error.value} {error.value!r} {error.value.args}"
    assert "secret-marker" not in text.lower()
    assert str(error.value) == error.value.code
    assert error.value.__cause__ is None and error.value.__context__ is None
