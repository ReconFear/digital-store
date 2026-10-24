"""PR6: защита изменяющих запросов — Origin, Content-Type, CSRF (accounts/request_guard.py; PR6_DESIGN_REV4 §6.2,
REV5 X6, ADDENDUM §3). Только офлайн — без базы и сети. Ключи и токены синтетические.
"""

import ast
from pathlib import Path

import pytest
from starlette.datastructures import Headers

import accounts.request_guard as guard
from accounts.request_guard import (
    CSRF_HEADER, OriginAllowlist, OriginError, RequestRefused, csrf_token, guard_credentials_request,
    guard_logout_request, guard_session_request, parse_origin, verify_csrf_token,
)
from accounts.session import SessionToken

CSRF_KEY = bytes(range(64, 96))
OTHER_KEY = bytes(range(96, 128))
ALLOW = OriginAllowlist(["http://localhost:5500", "https://shop.example", "http://[::1]:5173"])
GOOD = {"Origin": "http://localhost:5500", "Content-Type": "application/json"}


def _headers(**override):
    raw = dict(GOOD)
    raw.update({k.replace("_", "-"): v for k, v in override.items()})
    return Headers(raw=[(k.lower().encode(), v.encode()) for k, v in raw.items() if v is not None])


def _refused(fn, *args):
    with pytest.raises(RequestRefused) as error:
        fn(*args)
    assert error.value.http_status == 403 and error.value.code == "csrf_failed"
    return error.value.reason


# ---------- Origin ----------

@pytest.mark.parametrize("origin, expected", [
    ("http://localhost:5500", ("http", "localhost", 5500)),
    ("https://shop.example", ("https", "shop.example", 443)),
    ("https://shop.example:443", ("https", "shop.example", 443)),
    ("http://shop.example", ("http", "shop.example", 80)),
    ("http://127.0.0.1:5500", ("http", "127.0.0.1", 5500)),
    ("http://[::1]:5173", ("http", "[::1]", 5173)),
    ("http://xn--e1afmkfd.example", ("http", "xn--e1afmkfd.example", 80)),
])
def test_parse_origin_canonical_form(origin, expected):
    assert parse_origin(origin) == expected


@pytest.mark.parametrize("origin", [
    "", "null", "*", " http://localhost:5500", "http://localhost:5500 ", "http://localhost:5500/",
    "http://localhost:5500/path", "http://localhost:5500?x=1", "http://localhost:5500#x", "http://user@localhost:5500",
    "http://user:pass@localhost:5500", "ftp://localhost", "file://", "localhost:5500", "//localhost:5500",
    "http://localhost:", "http://localhost:99999", "http://localhost:0", "http://localhost:abc", "http://exämple.test",
    "http://ex_ample.test", "http://localhost.", "http://[zz]:5500", "http://[::1", "HTTP://localhost:5500",
    "http://localhost:5500,http://evil.test", "http://localhost:5500 http://evil.test", "http://", None, 5500,
])
def test_invalid_origins_are_refused(origin):
    with pytest.raises(OriginError):
        parse_origin(origin)
    assert ALLOW.allows(origin) is False


@pytest.mark.parametrize("origin", [
    "http://localhost:5501",                 # другой порт
    "https://localhost:5500",                # другая схема
    "http://127.0.0.1:5500",                 # другой хост (тот же компьютер, но другой origin)
    "http://localhost",                      # порт по умолчанию 80, не 5500
    "http://localhost:5500.evil.test",       # не разбирается: точка после порта
    "http://evil-localhost:5500",            # суффикс
    "http://localhost.evil.test:5500",       # префикс
    "https://a.shop.example",                # поддомен разрешённого
    "https://shop.example.evil.test",        # разрешённый как префикс чужого
    "https://evilshop.example",
    "http://shop.example",                   # https разрешён, http — нет
    "http://[::2]:5173",
])
def test_allowlist_is_exact(origin):
    assert ALLOW.allows(origin) is False


@pytest.mark.parametrize("origin", ["http://localhost:5500", "https://shop.example", "https://shop.example:443",
                                    "http://[::1]:5173", "http://[0:0:0:0:0:0:0:1]:5173"])
def test_allowlist_accepts_the_same_origin_in_any_canonical_spelling(origin):
    assert ALLOW.allows(origin) is True


@pytest.mark.parametrize("entries", [["*"], ["null"], ["http://localhost:5500/"], ["localhost:5500"],
                                     ["http://a.test", "ftp://b.test"]])
def test_allowlist_rejects_bad_entries(entries):
    with pytest.raises(OriginError):
        OriginAllowlist(entries)


def test_allowlist_rejects_a_plain_string_and_empty_allows_nothing():
    with pytest.raises(TypeError):
        OriginAllowlist("http://localhost:5500")
    assert OriginAllowlist([]).allows("http://localhost:5500") is False


# ---------- Content-Type и Origin в запросе ----------

@pytest.mark.parametrize("content_type", ["application/json", "application/json; charset=utf-8", "Application/JSON",
                                          "application/json ;charset=UTF-8"])
def test_json_content_type_accepted(content_type):
    guard_credentials_request(_headers(Content_Type=content_type), ALLOW)


@pytest.mark.parametrize("content_type, reason", [
    (None, "content_type_missing"),
    ("text/plain", "content_type_not_json"),
    ("application/x-www-form-urlencoded", "content_type_not_json"),
    ("multipart/form-data; boundary=x", "content_type_not_json"),
    ("application/json-patch+json", "content_type_not_json"),
    ("application/jsonx", "content_type_not_json"),
    ("", "content_type_not_json"),
])
def test_non_json_content_type_refused(content_type, reason):
    assert _refused(guard_credentials_request, _headers(Content_Type=content_type), ALLOW) == reason


@pytest.mark.parametrize("origin, reason", [
    (None, "origin_missing"),
    ("null", "origin_forbidden"),
    ("https://evil.test", "origin_forbidden"),
    ("http://localhost:5501", "origin_forbidden"),
    ("http://localhost:5500/", "origin_forbidden"),
])
def test_missing_or_foreign_origin_refused(origin, reason):
    assert _refused(guard_credentials_request, _headers(Origin=origin), ALLOW) == reason


def test_duplicate_origin_or_content_type_headers_refused():
    two_origins = Headers(raw=[(b"origin", b"http://localhost:5500"), (b"origin", b"https://evil.test"),
                               (b"content-type", b"application/json")])
    assert _refused(guard_credentials_request, two_origins, ALLOW) == "origin_multiple"
    two_types = Headers(raw=[(b"origin", b"http://localhost:5500"), (b"content-type", b"application/json"),
                             (b"content-type", b"text/plain")])
    assert _refused(guard_credentials_request, two_types, ALLOW) == "content_type_multiple"


def test_plain_mapping_headers_with_any_case():
    guard_credentials_request({"ORIGIN": "http://localhost:5500", "content-TYPE": "application/json"}, ALLOW)
    with pytest.raises(RequestRefused):
        guard_credentials_request({"Content-Type": "application/json"}, ALLOW)


def test_guard_requires_an_allowlist_object():
    with pytest.raises(TypeError):
        guard_credentials_request(_headers(), ["http://localhost:5500"])


# ---------- CSRF ----------

def test_csrf_token_is_per_session_and_per_key():
    session, other = SessionToken.new(), SessionToken.new()
    token = csrf_token(CSRF_KEY, session)
    assert len(token) == 43 and "=" not in token
    assert token == csrf_token(CSRF_KEY, session)
    assert token != csrf_token(CSRF_KEY, other)
    assert token != csrf_token(OTHER_KEY, session)
    assert session.value not in token


def test_verify_csrf_token():
    session, other = SessionToken.new(), SessionToken.new()
    good = csrf_token(CSRF_KEY, session)
    assert verify_csrf_token(CSRF_KEY, session, good) is True
    tampered = ("A" if good[0] != "A" else "B") + good[1:]
    for forged in (csrf_token(CSRF_KEY, other), csrf_token(OTHER_KEY, session), tampered, good + "=", good[:-1],
                   "", None, 123, session.value, " " + good[1:]):
        assert verify_csrf_token(CSRF_KEY, session, forged) is False, forged


def test_csrf_token_with_trailing_newline_is_refused():
    """L-2: верный CSRF-токен с завершающим \\n отклоняется."""
    session = SessionToken.new()
    assert verify_csrf_token(CSRF_KEY, session, csrf_token(CSRF_KEY, session) + "\n") is False


def test_csrf_comparison_is_constant_time(monkeypatch):
    calls = []
    real = guard.hmac.compare_digest

    def spy(a, b):
        calls.append((type(a), type(b)))
        return real(a, b)

    monkeypatch.setattr(guard.hmac, "compare_digest", spy)
    session = SessionToken.new()
    verify_csrf_token(CSRF_KEY, session, csrf_token(CSRF_KEY, SessionToken.new()))
    assert calls == [(bytes, bytes)]


@pytest.mark.parametrize("key", [b"", bytes(31), bytes(33), "x" * 32, None])
def test_csrf_requires_a_32_byte_key(key):
    with pytest.raises(TypeError):
        csrf_token(key, SessionToken.new())
    with pytest.raises(TypeError):
        verify_csrf_token(key, SessionToken.new(), "A" * 43)


# ---------- политики ----------

def test_session_request_needs_csrf_of_this_session():
    session, other = SessionToken.new(), SessionToken.new()
    guard_session_request(_headers(**{"X-CSRF-Token": csrf_token(CSRF_KEY, session)}), ALLOW, CSRF_KEY, session)
    assert _refused(guard_session_request, _headers(), ALLOW, CSRF_KEY, session) == "csrf_missing"
    assert _refused(guard_session_request, _headers(**{"X-CSRF-Token": csrf_token(CSRF_KEY, other)}),
                    ALLOW, CSRF_KEY, session) == "csrf_invalid"
    assert _refused(guard_session_request, _headers(**{"X-CSRF-Token": "A" * 43}), ALLOW, CSRF_KEY,
                    session) == "csrf_invalid"
    assert _refused(guard_session_request, _headers(**{"X-CSRF-Token": csrf_token(CSRF_KEY, session)}),
                    ALLOW, CSRF_KEY, None) == "session_missing"
    # Верный токен не спасает чужой Origin или не-JSON.
    good = csrf_token(CSRF_KEY, session)
    assert _refused(guard_session_request, _headers(Origin="https://evil.test", **{"X-CSRF-Token": good}),
                    ALLOW, CSRF_KEY, session) == "origin_forbidden"
    assert _refused(guard_session_request, _headers(Content_Type="text/plain", **{"X-CSRF-Token": good}),
                    ALLOW, CSRF_KEY, session) == "content_type_not_json"


def test_logout_needs_origin_and_json_but_no_csrf_token():
    """REV5 X6: выход без CSRF-токена (его не получить при недействительной сессии), но межсайтовый —
    отклоняется проверкой Origin."""
    guard_logout_request(_headers(), ALLOW)
    assert _refused(guard_logout_request, _headers(Origin=None), ALLOW) == "origin_missing"
    assert _refused(guard_logout_request, _headers(Origin="https://evil.test"), ALLOW) == "origin_forbidden"
    assert _refused(guard_logout_request, _headers(Content_Type="text/plain"), ALLOW) == "content_type_not_json"


def test_credentials_request_does_not_need_csrf_token():
    guard_credentials_request(_headers(), ALLOW)


def test_refusal_reveals_nothing():
    session = SessionToken.new()
    token = csrf_token(CSRF_KEY, session)
    with pytest.raises(RequestRefused) as error:
        guard_session_request(_headers(Origin="https://evil.test", **{CSRF_HEADER: token}), ALLOW, CSRF_KEY, session)
    texts = [str(error.value), repr(error.value), str(error.value.as_detail())]
    for text in texts:
        assert token not in text and session.value not in text and "evil.test" not in text
        assert CSRF_KEY.hex() not in text
    assert error.value.as_detail() == {"code": "csrf_failed", "message": "Запрос отклонён проверкой безопасности."}


def test_modules_do_not_load_dotenv_or_touch_the_database():
    """accounts.session/request_guard не импортируют cors_settings (load_dotenv при импорте) и database."""
    root = Path(__file__).resolve().parent.parent / "accounts"
    for name in ("session.py", "request_guard.py"):
        tree = ast.parse((root / name).read_text(encoding="utf-8"))
        imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                    for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.module}
        assert imported <= {"base64", "hashlib", "hmac", "ipaddress", "re", "secrets", "urllib", "accounts"}, name
