"""PR6: настройки аккаунтов (accounts/config.py). Только офлайн — без базы и сети.

Файлы секретов — синтетические, во временной папке pytest; окружение передаётся словарём (os.environ не
читается и не меняется). Проверяется: аккаунты выключены по умолчанию и тогда файл не читается; секреты —
только из DS_ACCOUNT_SECRETS_FILE; формат и версии перца; ошибки и repr без значений секретов.
"""

import base64

import pytest

from accounts.config import (
    CSRF_KEY, ENV_ENABLED, ENV_SECRETS_FILE, PEPPER_CURRENT, PEPPER_PREVIOUS, AccountConfigError, AccountSettings,
    Pepper, load_account_settings, parse_enabled,
)

SECRET_A = bytes(range(32))
SECRET_B = bytes(range(32, 64))
SECRET_C = bytes(range(64, 96))


def _b64(raw, padded=False):
    text = base64.urlsafe_b64encode(raw).decode("ascii")
    return text if padded else text.rstrip("=")


def _secrets_file(tmp_path, **values):
    path = tmp_path / "account-secrets.env"
    path.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8")
    return str(path)


def _valid(tmp_path, **override):
    values = {PEPPER_CURRENT: f"2:{_b64(SECRET_A)}", PEPPER_PREVIOUS: f"1:{_b64(SECRET_B, padded=True)}",
              CSRF_KEY: _b64(SECRET_C)}
    values.update(override)
    values = {k: v for k, v in values.items() if v is not None}
    return {ENV_ENABLED: "on", ENV_SECRETS_FILE: _secrets_file(tmp_path, **values)}


# ---------- выключено по умолчанию ----------

@pytest.mark.parametrize("env", [{}, {ENV_ENABLED: ""}, {ENV_ENABLED: "off"}, {ENV_ENABLED: "false"},
                                 {ENV_ENABLED: "0"}, {ENV_ENABLED: "no"}])
def test_accounts_are_disabled_by_default(env):
    assert load_account_settings(env) == AccountSettings(enabled=False)


def test_disabled_accounts_do_not_read_the_secrets_file(tmp_path):
    missing = str(tmp_path / "no-such-file.env")
    assert load_account_settings({ENV_ENABLED: "off", ENV_SECRETS_FILE: missing}).enabled is False


@pytest.mark.parametrize("value", ["onn", "enabled", "2", "yes please"])
def test_typo_in_flag_is_an_error_not_a_silent_enable(value):
    with pytest.raises(AccountConfigError):
        parse_enabled(value)


# ---------- включено: файл секретов ----------

def test_valid_secrets_file(tmp_path):
    settings = load_account_settings(_valid(tmp_path))
    assert settings.enabled is True
    assert settings.pepper_current == Pepper(2, SECRET_A)
    assert settings.pepper_previous == Pepper(1, SECRET_B)
    assert settings.csrf_key == SECRET_C


def test_previous_pepper_is_optional(tmp_path):
    settings = load_account_settings(_valid(tmp_path, **{PEPPER_PREVIOUS: None}))
    assert settings.pepper_previous is None and settings.pepper_current.version == 2


@pytest.mark.parametrize("env_update, message", [
    ({ENV_SECRETS_FILE: ""}, ENV_SECRETS_FILE),
    ({ENV_SECRETS_FILE: "Z:/no/such/account-secrets.env"}, "файл не найден"),
])
def test_enabled_accounts_require_the_secrets_file(tmp_path, env_update, message):
    env = _valid(tmp_path)
    env.update(env_update)
    with pytest.raises(AccountConfigError, match=message):
        load_account_settings(env)


@pytest.mark.parametrize("override, message", [
    ({PEPPER_CURRENT: None}, PEPPER_CURRENT),
    ({CSRF_KEY: None}, CSRF_KEY),
    ({PEPPER_CURRENT: _b64(SECRET_A)}, "<версия>:<base64url>"),             # без номера версии
    ({PEPPER_CURRENT: f"0:{_b64(SECRET_A)}"}, "версия должна быть"),
    ({PEPPER_CURRENT: f"32768:{_b64(SECRET_A)}"}, "<версия>:<base64url>|версия должна быть"),
    ({PEPPER_CURRENT: f"-1:{_b64(SECRET_A)}"}, "<версия>:<base64url>"),
    ({PEPPER_CURRENT: f"2:{_b64(SECRET_A[:31])}"}, "base64url"),             # 31 байт
    ({PEPPER_CURRENT: f"2:{base64.b64encode(bytes([251] * 32)).decode()}"}, "base64url"),  # '+' и '/' — не base64url
    ({CSRF_KEY: "not-a-key"}, "base64url"),
    ({PEPPER_PREVIOUS: f"2:{_b64(SECRET_B)}"}, "меньше"),                    # та же версия
    ({PEPPER_PREVIOUS: f"3:{_b64(SECRET_B)}"}, "меньше"),                    # версия новее текущей
    ({PEPPER_PREVIOUS: f"1:{_b64(SECRET_A)}"}, "разными"),                   # тот же секрет, что текущий
    ({CSRF_KEY: _b64(SECRET_A)}, "разными"),                                 # CSRF-ключ = перец
    ({"ACCOUNT_PEPER_PREVIOUS": f"1:{_b64(SECRET_B)}"}, "неизвестные имена"),  # опечатка в имени
])
def test_invalid_secrets_are_refused(tmp_path, override, message):
    with pytest.raises(AccountConfigError, match=message):
        load_account_settings(_valid(tmp_path, **override))


@pytest.mark.parametrize("name", [PEPPER_CURRENT, PEPPER_PREVIOUS, CSRF_KEY])
@pytest.mark.parametrize("enabled", ["on", "off"])
def test_secrets_in_process_environment_are_refused(tmp_path, name, enabled):
    """Секреты не должны попадать в .env, который читают и другие процессы (REV4 §1.3)."""
    env = _valid(tmp_path)
    env[ENV_ENABLED] = enabled
    env[name] = f"1:{_b64(SECRET_B)}"
    with pytest.raises(AccountConfigError, match="окружении процесса"):
        load_account_settings(env)


# ---------- значения секретов не попадают в сообщения и repr ----------

def test_errors_and_repr_do_not_contain_secret_values(tmp_path):
    settings = load_account_settings(_valid(tmp_path))
    text = repr(settings)
    for raw in (SECRET_A, SECRET_B, SECRET_C):
        assert repr(raw) not in text and _b64(raw) not in text and raw.hex() not in text
    assert "version=2" in text and "version=1" in text

    leaked = f"2:{_b64(SECRET_A[:31])}"
    with pytest.raises(AccountConfigError) as error:
        load_account_settings(_valid(tmp_path, **{PEPPER_CURRENT: leaked}))
    assert _b64(SECRET_A[:31]) not in str(error.value)
    with pytest.raises(AccountConfigError) as error:
        load_account_settings({**_valid(tmp_path), CSRF_KEY: _b64(SECRET_C)})
    assert _b64(SECRET_C) not in str(error.value)
