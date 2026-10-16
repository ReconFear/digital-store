"""PR5: связка ключей, AES-256-GCM с AAD, отпечатки, проверочные значения; worker без секретов.
Без базы данных. Секреты — тестовые (digital_keys_testkit)."""

import base64
import uuid

import pytest

from commerce import fulfillment_worker
from commerce.digital_keys.crypto import (
    DecryptionError, InvalidCode, Keyring, KeyringError, check_value, normalize_code,
)
from digital_keys_testkit import TEST_ENV, keyring

PID = 7
PUB = str(uuid.uuid4())


def test_roundtrip_and_random_nonce():
    ring = keyring()
    key_id, nonce, ct = ring.encrypt("ABC-def-123", PID, PUB)
    assert key_id == "t1" and len(nonce) == 12 and len(ct) == len("ABC-def-123") + 16
    assert ring.decrypt(PID, PUB, key_id, nonce, ct) == "ABC-def-123"
    assert ring.encrypt("ABC-def-123", PID, PUB)[1] != nonce


@pytest.mark.parametrize("change", ["product", "public_id", "key_id", "ciphertext", "tag", "nonce"])
def test_aad_and_tag_detect_any_change(change):
    ring = keyring()
    key_id, nonce, ct = ring.encrypt("SECRET-1", PID, PUB)
    args = {"product_id": PID, "public_id": PUB, "key_id": key_id, "nonce": nonce, "ciphertext": ct}
    if change == "product":
        args["product_id"] = PID + 1
    elif change == "public_id":
        args["public_id"] = str(uuid.uuid4())
    elif change == "key_id":
        args["key_id"] = "t2"          # другая версия: тот же текст, другой секрет и AAD
    elif change == "ciphertext":
        args["ciphertext"] = bytes([ct[0] ^ 1]) + ct[1:]
    elif change == "tag":
        args["ciphertext"] = ct[:-1] + bytes([ct[-1] ^ 1])
    else:
        args["nonce"] = bytes([nonce[0] ^ 1]) + nonce[1:]
    with pytest.raises(DecryptionError) as error:
        ring.decrypt(**args)
    assert "SECRET-1" not in str(error.value)


def test_unknown_version_and_missing_settings():
    ring = keyring()
    key_id, nonce, ct = ring.encrypt("X", PID, PUB)
    with pytest.raises(DecryptionError):
        ring.decrypt(PID, PUB, "nope", nonce, ct)
    with pytest.raises(KeyringError, match="не задан DIGITAL_KEYS_ENCRYPTION_KEYS"):
        Keyring.from_env({})
    with pytest.raises(KeyringError, match="DIGITAL_KEYS_ACTIVE_KEY"):
        Keyring.from_env({"DIGITAL_KEYS_ENCRYPTION_KEYS": TEST_ENV["DIGITAL_KEYS_ENCRYPTION_KEYS"]}, need_active=True)
    with pytest.raises(KeyringError, match="DIGITAL_KEYS_FINGERPRINT_KEY"):
        Keyring.from_env({k: v for k, v in TEST_ENV.items() if k != "DIGITAL_KEYS_FINGERPRINT_KEY"},
                         need_fingerprint=True)


@pytest.mark.parametrize("value, match", [
    ("k1", "формат"),
    ("K1:" + base64.b64encode(b"x" * 32).decode(), "формат"),
    ("k1:not-base64!!", "base64"),
    ("k1:" + base64.b64encode(b"x" * 16).decode(), "32 байта"),
    ("k1:" + base64.b64encode(b"x" * 32).decode() + ",k1:" + base64.b64encode(b"y" * 32).decode(), "дважды"),
])
def test_bad_settings_fail_closed_without_values(value, match):
    with pytest.raises(KeyringError, match=match) as error:
        Keyring.from_env({"DIGITAL_KEYS_ENCRYPTION_KEYS": value})
    assert base64.b64encode(b"y" * 32).decode() not in str(error.value)


def test_fingerprint_secret_must_differ_from_encryption_key():
    secret = TEST_ENV["DIGITAL_KEYS_ENCRYPTION_KEYS"].split(",")[0].split(":", 1)[1]
    with pytest.raises(KeyringError, match="разные"):
        Keyring.from_env({"DIGITAL_KEYS_ENCRYPTION_KEYS": TEST_ENV["DIGITAL_KEYS_ENCRYPTION_KEYS"],
                          "DIGITAL_KEYS_FINGERPRINT_KEY": f"f1:{secret}"})


def test_repr_hides_secrets():
    ring = keyring()
    text = repr(ring)
    for part in TEST_ENV.values():
        for piece in part.split(","):
            if ":" in piece:
                assert piece.split(":", 1)[1] not in text


def test_fingerprint_is_keyed_and_case_sensitive():
    ring = keyring()
    assert ring.fingerprint("AbC") == ring.fingerprint("AbC")
    assert ring.fingerprint("AbC") != ring.fingerprint("abc")
    other = Keyring.from_env({**TEST_ENV, "DIGITAL_KEYS_FINGERPRINT_KEY": "tf1:" + base64.b64encode(b"z" * 32).decode()})
    assert other.fingerprint("AbC") != ring.fingerprint("AbC")


def test_check_values_and_registry_verification():
    ring = keyring()
    values = ring.check_values()
    assert set(values) == {"encryption:t1", "encryption:t2", "fingerprint:tf1"}
    assert all(len(bytes.fromhex(v)) == 16 for v in values.values())
    assert check_value(b"a" * 32, "encryption", "k1") != check_value(b"a" * 32, "fingerprint", "k1")
    ring.verify_against(values, ["encryption:t1"])
    with pytest.raises(KeyringError, match="не зарегистрирована"):
        ring.verify_against({}, ["encryption:t1"])
    with pytest.raises(KeyringError, match="НЕ совпадает"):
        ring.verify_against({**values, "encryption:t1": "00" * 16}, ["encryption:t1"])
    with pytest.raises(KeyringError, match="нет версии"):
        ring.verify_against(values, ["encryption:t9"])


def test_secrets_file_is_read_and_env_overrides(tmp_path):
    path = tmp_path / "secrets.env"
    path.write_text("\n".join(f"{k}={v}" for k, v in TEST_ENV.items()) + "\n", encoding="utf-8")
    ring = Keyring.from_env({"DIGITAL_KEYS_SECRETS_FILE": str(path)}, need_active=True, need_fingerprint=True)
    assert ring.active == "t1" and set(ring.encryption) == {"t1", "t2"}
    ring = Keyring.from_env({"DIGITAL_KEYS_SECRETS_FILE": str(path), "DIGITAL_KEYS_ACTIVE_KEY": "t2"})
    assert ring.active == "t2"
    with pytest.raises(KeyringError, match="файл не найден"):
        Keyring.from_env({"DIGITAL_KEYS_SECRETS_FILE": str(tmp_path / "missing.env")})


@pytest.mark.parametrize("raw, expected", [
    ("ABC-123", "ABC-123"),
    ("﻿ABC-123\r\n", "ABC-123"),
    ("  \tAbc def\t ", "Abc def"),
    ("a\tb", "a\tb"),
    ("", None),
    ("  \r\n", None),
    ("ключ-Ё", "ключ-Ё"),
])
def test_normalize(raw, expected):
    assert normalize_code(raw) == expected


@pytest.mark.parametrize("raw", ["a\x00b", "a\nb", "a​b", "a‮b", "x" * 513, "a\x7fb"])
def test_normalize_rejects(raw):
    with pytest.raises(InvalidCode) as error:
        normalize_code(raw)
    assert raw not in str(error.value)


# ---------- worker без секретов ----------

def test_worker_refuses_to_start_with_encryption_secrets(capsys):
    for name in ("DIGITAL_KEYS_ENCRYPTION_KEYS", "DIGITAL_KEYS_FINGERPRINT_KEY", "DIGITAL_KEYS_SECRETS_FILE"):
        code = fulfillment_worker.main(["--once"], env={"FULFILLMENT_MODULES": "digital_keys", name: "x"})
        assert code == 2
        assert "не должен получать секреты" in capsys.readouterr().err


def test_worker_modules_are_explicit(capsys):
    assert fulfillment_worker.main(["--once"], env={}) == 0
    assert "FULFILLMENT_MODULES пуст" in capsys.readouterr().out
    assert fulfillment_worker.main(["--once"], env={"FULFILLMENT_MODULES": "teleport"}) == 2
    assert fulfillment_worker.configured_modules({"FULFILLMENT_MODULES": " digital_keys "}) == ["digital_keys"]


def test_worker_module_does_not_import_crypto():
    import subprocess
    import sys
    code = ("import sys; import commerce.digital_keys.handler, commerce.fulfillment_worker; "
            "print('commerce.digital_keys.crypto' in sys.modules, 'cryptography' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env=dict(__import__("os").environ)).stdout.split()
    assert out == ["False", "False"]
