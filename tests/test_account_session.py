"""PR6: токен сессии и cookie (accounts/session.py, PR6_DESIGN_REV4 §6.1). Только офлайн — без базы и сети.

Совместимость с будущими маршрутами проверяется на настоящих объектах Starlette (Response), атрибуты
cookie — разбором стандартным http.cookies.
"""

import hashlib
import pickle
from http.cookies import SimpleCookie

import pytest
from starlette.responses import Response

from accounts.session import (
    SESSION_COOKIE_MAX_AGE, SESSION_COOKIE_NAME, SessionToken, clear_session_cookie, clear_session_cookie_header,
    has_session_cookie, session_cookie_header, session_from_cookies, set_session_cookie,
)


def _attributes(header):
    """Set-Cookie -> (имя, значение, {атрибут: значение}) с флагами без значения."""
    parts = [p.strip() for p in header.split(";")]
    name, _, value = parts[0].partition("=")
    attributes = {}
    for part in parts[1:]:
        key, sep, val = part.partition("=")
        attributes[key.lower()] = val if sep else True
    return name, value, attributes


# ---------- токен ----------

def test_new_token_is_256_bit_urlsafe_and_unique():
    tokens = {SessionToken.new().value for _ in range(200)}
    assert len(tokens) == 200
    assert all(len(t) == 43 and set(t) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")
               for t in tokens)


def test_database_receives_only_sha256():
    token = SessionToken.new()
    assert token.sha256 == hashlib.sha256(token.value.encode("ascii")).digest()
    assert len(token.sha256) == 32
    assert token.value.encode("ascii") not in token.sha256


def test_token_is_hidden_in_repr_str_format_and_pickle():
    token = SessionToken.new()
    for text in (repr(token), str(token), f"{token}", f"{token!r}", f"{token:>60}", "%s" % token, repr([token]),
                 repr({"session": token})):
        assert token.value not in text
    with pytest.raises(TypeError):
        pickle.dumps(token)


@pytest.mark.parametrize("value", ["", "short", "A" * 42, "A" * 44, "A" * 42 + "=", "A" * 42 + "+", "A" * 42 + "/",
                                   " " + "A" * 42, "A" * 42 + "\n", None, 123, b"A" * 43])
def test_invalid_token_values_are_refused_without_echo(value):
    with pytest.raises(ValueError) as error:
        SessionToken(value)
    if isinstance(value, str) and value:
        assert value.strip() not in str(error.value)


def test_token_with_trailing_newline_is_refused():
    """L-2: 43 допустимых символа и завершающий \\n — не токен (fullmatch, а не match с '$')."""
    with pytest.raises(ValueError):
        SessionToken("A" * 43 + "\n")


# ---------- cookie запроса ----------

def test_session_from_cookies():
    token = SessionToken.new()
    found = session_from_cookies({SESSION_COOKIE_NAME: token.value})
    assert found is not None and found.value == token.value
    assert session_from_cookies({}) is None
    assert session_from_cookies(None) is None
    assert session_from_cookies({"ds_session": token.value}) is None            # без префикса __Host- — не наша
    assert session_from_cookies({SESSION_COOKIE_NAME: "x" * 10}) is None        # неверный формат в базу не идёт


def test_cookie_token_with_trailing_newline_is_ignored():
    """L-2: значение cookie из 43 допустимых символов с завершающим \\n — не сессия."""
    assert session_from_cookies({SESSION_COOKIE_NAME: "A" * 43 + "\n"}) is None


def test_has_session_cookie_sees_even_a_malformed_cookie():
    assert has_session_cookie({SESSION_COOKIE_NAME: "garbage"}) is True
    assert has_session_cookie({SESSION_COOKIE_NAME: ""}) is True
    assert has_session_cookie({"other": "x"}) is False
    assert has_session_cookie(None) is False


# ---------- Set-Cookie ----------

def test_session_cookie_has_exact_host_prefix_attributes():
    token = SessionToken.new()
    header = session_cookie_header(token)
    name, value, attributes = _attributes(header)
    assert (name, value) == (SESSION_COOKIE_NAME, token.value)
    assert attributes == {"max-age": str(SESSION_COOKIE_MAX_AGE), "path": "/", "secure": True, "httponly": True,
                          "samesite": "Strict"}
    assert "domain" not in header.lower() and "expires" not in attributes
    # Стандартный разбор видит те же атрибуты.
    morsel = SimpleCookie(header)[SESSION_COOKIE_NAME]
    assert morsel.value == token.value and morsel["path"] == "/" and morsel["secure"] and morsel["httponly"]
    assert morsel["samesite"] == "Strict" and morsel["domain"] == ""


def test_clear_cookie_uses_the_same_attributes_and_expires_now():
    name, value, attributes = _attributes(clear_session_cookie_header())
    assert (name, value) == (SESSION_COOKIE_NAME, "")
    assert attributes["max-age"] == "0" and attributes["expires"] == "Thu, 01 Jan 1970 00:00:00 GMT"
    assert {k: attributes[k] for k in ("path", "secure", "httponly", "samesite")} == {
        "path": "/", "secure": True, "httponly": True, "samesite": "Strict"}
    assert "domain" not in clear_session_cookie_header().lower()


@pytest.mark.parametrize("max_age", [0, -1, True, 1.5, "3600", 366 * 24 * 60 * 60 + 1])
def test_invalid_max_age_is_refused(max_age):
    with pytest.raises(ValueError):
        session_cookie_header(SessionToken.new(), max_age)


def test_cookie_header_accepts_only_session_token_objects():
    with pytest.raises(TypeError):
        session_cookie_header("A" * 43)


def test_starlette_response_gets_both_clear_and_new_cookie():
    """Смена пароля: удалить старую cookie нельзя иначе, как перезаписать новой; оба заголовка сохраняются."""
    response = Response()
    token = SessionToken.new()
    clear_session_cookie(response)
    set_session_cookie(response, token, max_age=3600)
    cookies = [v.decode("latin-1") for k, v in response.raw_headers if k == b"set-cookie"]
    assert cookies == [clear_session_cookie_header(), session_cookie_header(token, 3600)]
