"""Подготовка и загрузка партии ключей (процесс администратора).

Шифрование и отпечатки считаются здесь, в процессе, где есть оба секрета. В базу уходят только
шифротексты, nonce, отпечатки и UUID; открытые ключи товара — нет. Атомарность партии, дубликаты,
права и учёт остатка проверяет база (admin_import_digital_keys -> digital_keys_import_core).
"""

import json
import re
import uuid
from dataclasses import dataclass

from commerce.digital_keys.crypto import InvalidCode, KeyringError, normalize_code

MAX_BATCH = 10000
IMPORT_SQL = ("SELECT admin_import_digital_keys(%s, %s, %s::text[], %s::text[], %s::text[], %s::text[], "
              "%s::text[], %s, %s)")
# Только для тестов под владельцем схемы (обёртка admin_* владельцу отказывает).
IMPORT_CORE_SQL = ("SELECT digital_keys_import_core(%s, %s, %s::text[], %s::text[], %s::text[], %s::text[], "
                   "%s::text[], %s, %s, %s)")


class ImportProblem(Exception):
    """Партия не годится. Сообщение без значений ключей (только номера строк)."""


@dataclass
class PreparedBatch:
    product_id: int
    lines: list           # номер строки файла для каждого элемента партии
    public_ids: list
    enc_key_ids: list
    nonces_hex: list
    ciphertexts_hex: list
    fingerprints_hex: list
    fingerprint_key_id: str

    def __len__(self):
        return len(self.public_ids)

    def params(self):
        return (self.public_ids, self.enc_key_ids, self.nonces_hex, self.ciphertexts_hex,
                self.fingerprints_hex, self.fingerprint_key_id)


def read_codes(lines):
    """[(номер строки, ключ)] из итерируемых строк. Пустые строки пропускаются."""
    codes, bad = [], []
    for number, raw in enumerate(lines, start=1):
        try:
            code = normalize_code(raw)
        except InvalidCode as exc:
            bad.append(f"строка {number}: {exc}")
            continue
        if code is not None:
            codes.append((number, code))
    if bad:
        raise ImportProblem("недопустимые строки — партия не загружена:\n  " + "\n  ".join(bad[:50])
                            + ("\n  ..." if len(bad) > 50 else ""))
    if not codes:
        raise ImportProblem("в файле нет ключей")
    if len(codes) > MAX_BATCH:
        raise ImportProblem(f"в одной партии не больше {MAX_BATCH} ключей (сейчас {len(codes)})")
    return codes


# Разделители строк файла — только CRLF, LF и одиночный CR. str.splitlines() здесь НЕЛЬЗЯ: он режет
# и по U+2028/U+2029, U+0085, VT, FF, FS/GS/RS — такие символы внутри кода превращали бы один ключ
# в два. Здесь они остаются внутри строки и отклоняются normalize_code() вместе со всей партией.
LINE_BREAK = re.compile(r"\r\n|\n|\r")


def split_lines(text):
    return LINE_BREAK.split(text)


def read_codes_file(path):
    # utf-8-sig: BOM в начале файла (Блокнот Windows) не становится частью первого ключа.
    # newline="": Python не преобразует окончания строк — их разбирает split_lines().
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return read_codes(split_lines(handle.read()))


def prepare_batch(keyring, product_id, codes):
    if keyring.active is None or keyring.fingerprint_secret is None:
        raise KeyringError("для импорта нужны активный ключ шифрования и секрет отпечатков")
    batch = PreparedBatch(product_id, [], [], [], [], [], [], keyring.fingerprint_id)
    for number, code in codes:
        public_id = str(uuid.uuid4())
        key_id, nonce, ciphertext = keyring.encrypt(code, product_id, public_id)
        batch.lines.append(number)
        batch.public_ids.append(public_id)
        batch.enc_key_ids.append(key_id)
        batch.nonces_hex.append(nonce.hex())
        batch.ciphertexts_hex.append(ciphertext.hex())
        batch.fingerprints_hex.append(keyring.fingerprint(code).hex())
    return batch


def _jsonb(value):
    return json.loads(value) if isinstance(value, str) else value


def submit(cursor, batch, source_label, skip_duplicates=False, *, core_actor=None):
    """Загружает партию в ТЕКУЩЕЙ транзакции cursor. Отчёт базы с номерами строк вместо индексов.

    core_actor задают только тесты под владельцем схемы (вызов ядра без проверки администратора).
    """
    if core_actor is None:
        cursor.execute(IMPORT_SQL, (batch.product_id, source_label, *batch.params(), skip_duplicates))
    else:
        cursor.execute(IMPORT_CORE_SQL, (batch.product_id, source_label, *batch.params(), skip_duplicates,
                                         core_actor))
    report = _jsonb(cursor.fetchone()[0])
    for name in ("duplicates_in_batch", "duplicates_existing"):
        report[name + "_lines"] = [batch.lines[i - 1] for i in report.get(name, [])]
    return report
