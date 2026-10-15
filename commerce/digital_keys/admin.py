"""CLI администратора цифровых ключей.

    python -m commerce.digital_keys.admin <команда> [параметры]

Подключение — ПЕРСОНАЛЬНОЙ учётной записью администратора (член группы <prefix>_admin), не ролью
веб-приложения, worker или мигратора и не суперпользователем:
    ADMIN_DB_USER (обязательно), ADMIN_DB_PASSWORD (если не задан — спросит), ADMIN_DB_HOST,
    ADMIN_DB_PORT, ADMIN_DB_NAME (по умолчанию — как DB_HOST, DB_PORT, DB_NAME).
Права проверяет БАЗА (shop_require_admin в каждой admin_*-функции); проверка CLI в начале нужна
только для понятного сообщения. Каждое действие — отдельная транзакция.

Команды:
  generate-key [--id ID]           новый секрет (base64, 32 байта) — для DIGITAL_KEYS_*; в базу не пишет
  verify-keyring [--register]      сверить секреты процесса с реестром базы; проверить, что ими
                                   расшифровываются уже загруженные ключи; --register — внести
                                   новые версии в реестр
  import-keys --product ID --file PATH --label TEXT [--skip-duplicates]
  stock [--product ID]             запас: свободно, назначено, изъято, ждут выдачи, остаток, запас сверх
  issues                           задачи выдачи, требующие внимания
  resume --fulfillment ID --reason TEXT [--release-refund-hold]
  cancel --fulfillment ID --reason TEXT
  withdraw --key UUID --reason TEXT
  reconcile-stock --product ID     уменьшить остаток товара до обеспеченного ключами
  whoami                           кем вы вошли и есть ли права администратора

Значения ключей товара CLI никогда не печатает и не пишет в журналы.
"""

import argparse
import getpass
import json
import os
import sys

import psycopg

from commerce.digital_keys import importer
from commerce.digital_keys.crypto import DecryptionError, Keyring, KeyringError, generate_secret_b64, KEY_ID_RE


class CliError(Exception):
    pass


def _jsonb(value):
    return json.loads(value) if isinstance(value, str) else value


def admin_connect(env=None):
    env = os.environ if env is None else env
    user = env.get("ADMIN_DB_USER")
    if not user:
        raise CliError("задайте ADMIN_DB_USER — персональную учётную запись администратора (DIGITAL_KEYS.md)")
    password = env.get("ADMIN_DB_PASSWORD")
    if password is None and sys.stdin.isatty():
        password = getpass.getpass(f"Пароль PostgreSQL для {user}: ")
    return psycopg.connect(
        host=env.get("ADMIN_DB_HOST") or env.get("DB_HOST"),
        port=env.get("ADMIN_DB_PORT") or env.get("DB_PORT"),
        dbname=env.get("ADMIN_DB_NAME") or env.get("DB_NAME"),
        user=user,
        password=password,
    )


def _one(conn, sql, params=()):
    """Одно действие = одна транзакция."""
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            value = cursor.fetchone()[0]
        conn.commit()
        return value
    except BaseException:
        conn.rollback()
        raise


def _rows(conn, sql, params=()):
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        conn.commit()
        return rows
    except BaseException:
        conn.rollback()
        raise


def cmd_whoami(conn, args, out):
    out(f"Администратор магазина: {_one(conn, 'SELECT admin_whoami()')}")


def _keyring(**kw):
    return Keyring.from_env(**kw)


def cmd_verify_keyring(conn, args, out):
    keyring = _keyring(need_encryption=False)
    mine = keyring.check_values()
    if not mine:
        raise CliError("в окружении нет ни одной версии DIGITAL_KEYS_*: проверять нечего")
    registry = _jsonb(_one(conn, "SELECT encryption_key_check_values()")) or {}
    problems = 0
    for name, value in sorted(mine.items()):
        registered = registry.get(name)
        if registered == value:
            out(f"  {name}: совпадает с реестром")
        elif registered is not None:
            out(f"  {name}: НЕ СОВПАДАЕТ с реестром — это другой секрет. Не используйте его.")
            problems += 1
        elif args.register:
            purpose, key_id = name.split(":", 1)
            result = _one(conn, "SELECT admin_register_encryption_key(%s, %s, %s)", (purpose, key_id, value))
            out(f"  {name}: {'зарегистрирован' if result == 'registered' else 'уже зарегистрирован'}")
        else:
            out(f"  {name}: не зарегистрирован (повторите с --register)")
            problems += 1
    for name in sorted(set(registry) - set(mine)):
        out(f"  {name}: есть в реестре, но не задан в этом процессе")
    # Расшифровка образца каждой версии, которой зашифрованы ключи в базе.
    samples = _jsonb(_one(conn, "SELECT admin_digital_keys_keyring_sample()")) or []
    for sample in samples:
        key_id = sample["enc_key_id"]
        if key_id not in keyring.encryption:
            out(f"  ключей версии {key_id}: {sample['count']} — секрета этой версии в процессе НЕТ")
            problems += 1
            continue
        try:
            keyring.decrypt(int(sample["product_id"]), sample["public_id"], key_id,
                            bytes.fromhex(sample["nonce"]), bytes.fromhex(sample["ciphertext"]))
            out(f"  ключей версии {key_id}: {sample['count']} — образец расшифрован")
        except DecryptionError:
            out(f"  ключей версии {key_id}: {sample['count']} — образец НЕ расшифровывается")
            problems += 1
    if problems:
        raise CliError(f"проблем: {problems}")
    out("Связка ключей в порядке.")


def cmd_import(conn, args, out):
    keyring = _keyring(need_active=True, need_fingerprint=True)
    registry = _jsonb(_one(conn, "SELECT encryption_key_check_values()")) or {}
    keyring.verify_against(registry, [f"encryption:{keyring.active}", f"fingerprint:{keyring.fingerprint_id}"])
    codes = importer.read_codes_file(args.file)
    batch = importer.prepare_batch(keyring, args.product, codes)
    del codes
    try:
        with conn.cursor() as cursor:
            report = importer.submit(cursor, batch, args.label, args.skip_duplicates)
        if report["status"] != "imported":
            conn.rollback()
        else:
            conn.commit()
    except BaseException:
        conn.rollback()
        raise
    dup_batch, dup_existing = report["duplicates_in_batch_lines"], report["duplicates_existing_lines"]
    if report["status"] != "imported":
        out(f"Партия НЕ загружена: из {report['submitted']} строк повторы внутри файла — {len(dup_batch)}, "
            f"уже в базе — {len(dup_existing)}.")
        if dup_batch:
            out(f"  строки-повторы в файле: {_lines(dup_batch)}")
        if dup_existing:
            out(f"  строки, уже загруженные ранее: {_lines(dup_existing)}")
        out("  Исправьте файл или повторите с --skip-duplicates.")
        raise CliError("партия отклонена")
    out(f"Загружено ключей: {report['inserted']} из {report['submitted']} (партия {report['batch_id']}); "
        f"остаток товара увеличен на {report['quantity_added']}.")
    if dup_batch or dup_existing:
        out(f"  пропущены повторы: в файле — {_lines(dup_batch) or 'нет'}; уже в базе — {_lines(dup_existing) or 'нет'}")


def _lines(numbers):
    shown = ", ".join(str(n) for n in numbers[:30])
    return shown + (f" … (всего {len(numbers)})" if len(numbers) > 30 else "")


def cmd_stock(conn, args, out):
    sql = ("SELECT product_id, name, fulfillment_type, quantity, available, assigned, withdrawn, awaiting, surplus "
           "FROM digital_key_stock")
    params = ()
    if args.product:
        sql += " WHERE product_id = %s"
        params = (args.product,)
    rows = _rows(conn, sql + " ORDER BY product_id", params)
    out("товар | выдача | остаток Q | свободно A | назначено | изъято | ждут W | A−W−Q")
    for row in rows:
        warn = "  <- остаток не обеспечен ключами (reconcile-stock)" if row[2] == "digital_keys" and row[8] < 0 else ""
        out(f"{row[0]} {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]} | {row[6]} | {row[7]} | {row[8]}{warn}")


def cmd_issues(conn, args, out):
    rows = _rows(conn, "SELECT fulfillment_id, order_id, product_id, fulfillment_type, quantity, status, attempts, "
                       "coalesce(status_note, last_error, ''), refund_hold_released_at IS NOT NULL "
                       "FROM fulfillment_issues ORDER BY fulfillment_id")
    if not rows:
        out("Задач, требующих внимания, нет.")
    for row in rows:
        out(f"задача {row[0]}: заказ {row[1]}, товар {row[2]}, {row[3]} × {row[4]}, {row[5]}, попыток {row[6]}"
            f"{', остановка возврата снята' if row[8] else ''} — {row[7]}")


def cmd_resume(conn, args, out):
    result = _jsonb(_one(conn, "SELECT admin_resume_fulfillment(%s, %s, %s)",
                         (args.fulfillment, args.reason, args.release_refund_hold)))
    out(f"Задача {args.fulfillment}: {result['status']} — worker выполнит её в ближайший проход.")


def cmd_cancel(conn, args, out):
    _one(conn, "SELECT admin_cancel_fulfillment(%s, %s)", (args.fulfillment, args.reason))
    out(f"Задача {args.fulfillment} отменена. Остаток товара и учёт денег не изменены; "
        "возврат денег — отдельно (request_refund).")


def cmd_withdraw(conn, args, out):
    result = _jsonb(_one(conn, "SELECT admin_withdraw_digital_key(%s, %s)", (args.key, args.reason)))
    out(f"Ключ изъят (товар {result['product_id']}).")


def cmd_reconcile(conn, args, out):
    result = _jsonb(_one(conn, "SELECT admin_reconcile_digital_stock(%s)", (args.product,)))
    out(f"Товар {result['product_id']}: остаток {result['old_quantity']} -> {result['new_quantity']}.")


COMMANDS = {
    "whoami": cmd_whoami,
    "verify-keyring": cmd_verify_keyring,
    "import-keys": cmd_import,
    "stock": cmd_stock,
    "issues": cmd_issues,
    "resume": cmd_resume,
    "cancel": cmd_cancel,
    "withdraw": cmd_withdraw,
    "reconcile-stock": cmd_reconcile,
}


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m commerce.digital_keys.admin",
                                     description="Администрирование цифровых ключей Digital Store")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("generate-key", help="новый секрет для DIGITAL_KEYS_*")
    p.add_argument("--id", default="k1", help="версия (a-z, 0-9, _ и -), например k1 или f1")
    sub.add_parser("whoami")
    p = sub.add_parser("verify-keyring")
    p.add_argument("--register", action="store_true")
    p = sub.add_parser("import-keys")
    p.add_argument("--product", type=int, required=True)
    p.add_argument("--file", required=True)
    p.add_argument("--label", required=True, help="источник партии (поставщик, номер счёта), до 100 символов")
    p.add_argument("--skip-duplicates", action="store_true")
    p = sub.add_parser("stock")
    p.add_argument("--product", type=int)
    sub.add_parser("issues")
    p = sub.add_parser("resume")
    p.add_argument("--fulfillment", type=int, required=True)
    p.add_argument("--reason", required=True)
    p.add_argument("--release-refund-hold", action="store_true",
                   help="снять остановку выдачи из-за запроса возврата (после проверки, что возврата не будет)")
    p = sub.add_parser("cancel")
    p.add_argument("--fulfillment", type=int, required=True)
    p.add_argument("--reason", required=True)
    p = sub.add_parser("withdraw")
    p.add_argument("--key", required=True, help="UUID записи ключа (public_id)")
    p.add_argument("--reason", required=True)
    p = sub.add_parser("reconcile-stock")
    p.add_argument("--product", type=int, required=True)
    return parser


def main(argv=None, connect=admin_connect, out=print):
    args = build_parser().parse_args(argv)
    if args.command == "generate-key":
        if not KEY_ID_RE.match(args.id):
            print("--id: a-z, 0-9, _ и -, до 32 символов", file=sys.stderr)
            return 2
        out("# Новый секрет. Сохраните его в хранилище секретов и в резервной копии (DIGITAL_KEYS.md);")
        out("# потеря секрета шифрования = потеря всех ключей, зашифрованных этой версией.")
        out(f"{args.id}:{generate_secret_b64()}")
        return 0
    try:
        conn = connect()
    except CliError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(f"Не удалось подключиться к базе: {_db_message(exc)}", file=sys.stderr)
        return 2
    try:
        _one(conn, "SELECT admin_whoami()")   # понятная ошибка сразу; защита — в каждой функции базы
        COMMANDS[args.command](conn, args, out)
        return 0
    except (CliError, KeyringError, importer.ImportProblem) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Ошибка файла: {exc.strerror or exc}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(f"База отказала: {_db_message(exc)}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def _db_message(exc):
    diag = getattr(exc, "diag", None)
    primary = getattr(diag, "message_primary", None) if diag is not None else None
    return primary or str(exc).splitlines()[0]


if __name__ == "__main__":
    sys.exit(main())
