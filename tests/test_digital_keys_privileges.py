"""PR5: права PostgreSQL на выдачу ключей и административные функции (R8) под настоящими ролями.

Как и test_db_privileges.py, СОЗДАЁТ временные роли (dst_<случайно>_*; нестандартный префикс) и
удаляет их в конце: только при DS_TEST_CREATE_ROLES=1 и праве CREATEROLE у тестовой роли.
Владелец временной схемы — тестовая роль (в роли «мигратора»).
"""

import os
import secrets

import pytest

import database
from commerce.digital_keys import admin as admin_cli
from digital_keys_testkit import TEST_ENV, delivery_keyring, jsonb, keyring, unique_codes
from test_db_privileges import _Role, _drop_roles, as_role, grant_connect, make_schema, shop_roles  # noqa: F401
from test_migrations import _run


def _owner_value(shop, sql, params=None):
    conn = shop["owner"]
    with conn.cursor() as cur:
        cur.execute(sql, params)
        value = cur.fetchone()[0] if cur.description else None
    conn.commit()
    return value


def _new_role(shop, suffix, sql_tail="LOGIN"):
    name = f"{shop['app'].name.rsplit('_', 1)[0]}_{suffix}"
    password = secrets.token_urlsafe(16)
    _run(shop["owner"], f"CREATE ROLE {name} {sql_tail} PASSWORD '{password}'")
    shop.setdefault("extra_roles", []).append(name)
    # Своя CONNECT: посторонняя роль, роль без наследования (CONNECT группы не наследуется) и т. п.
    grant_connect(shop["owner"], [name])
    return _Role(name, password, shop["schema"])


@pytest.fixture
def shop(shop_roles):
    """Роли магазина + дополнительные роли теста (удаляются до ролей магазина)."""
    yield shop_roles
    conn = shop_roles["owner"]
    conn.rollback()
    if shop_roles.get("extra_roles"):
        # Права дополнительных ролей — временная схема, членство в группе и CONNECT на базу.
        _drop_roles(conn, shop_roles["schema"], shop_roles["extra_roles"])


def _product(shop, quantity=0, fulfillment="digital_keys"):
    return _owner_value(shop, "INSERT INTO products (name, price_minor, quantity, is_for_sale, product_type, "
                              "inventory_mode, fulfillment_type) VALUES ('K', 10000, %s, TRUE, 'digital_key', "
                              "'tracked', %s) RETURNING id", (quantity, fulfillment))


def _cli(role, monkeypatch, *argv):
    for name, value in TEST_ENV.items():
        monkeypatch.setenv(name, value)
    lines = []
    code = admin_cli.main(list(argv), connect=role.connect, out=lines.append)
    return code, "\n".join(lines)


def _import(shop, monkeypatch, pid, codes, tmp_path, *extra):
    path = tmp_path / f"keys-{secrets.token_hex(4)}.txt"
    path.write_text("\n".join(codes) + "\n", encoding="utf-8")
    return _cli(shop["admin"], monkeypatch, "import-keys", "--product", str(pid), "--file", str(path),
                "--label", "поставщик", *extra)


ADMIN_CALLS = [
    "SELECT admin_whoami()",
    "SELECT admin_withdraw_digital_key(gen_random_uuid(), 'x')",
    "SELECT admin_resume_fulfillment(1, 'x', FALSE)",
    "SELECT admin_cancel_fulfillment(1, 'x')",
    "SELECT admin_reconcile_digital_stock(1)",
    "SELECT admin_register_encryption_key('encryption', 'zz', repeat('00', 16))",
    "SELECT admin_digital_keys_keyring_sample()",
    "SELECT admin_import_digital_keys(1, 'x', ARRAY['00000000-0000-0000-0000-000000000000'], ARRAY['t1'], "
    "ARRAY[repeat('00', 12)], ARRAY[repeat('00', 17)], ARRAY[repeat('00', 32)], 'tf1', FALSE)",
]


# ---------- полный путь под раздельными ролями ----------

def test_full_flow_under_roles(shop, as_role, monkeypatch, tmp_path):
    from commerce.digital_keys.delivery import get_delivery
    from payments.access import create_order_with_access
    from digital_keys_testkit import run_worker

    code, out = _cli(shop["admin"], monkeypatch, "verify-keyring", "--register")
    assert code == 0, out
    pid = _product(shop)
    codes = unique_codes(3)
    code, out = _import(shop, monkeypatch, pid, codes, tmp_path)
    assert code == 0 and "Загружено ключей: 3 из 3" in out, out
    assert all(c not in out for c in codes)
    assert _owner_value(shop, "SELECT imported_by FROM digital_key_batches WHERE product_id = %s", (pid,)) == \
        shop["admin"].name

    as_role(shop["app"])
    order, token = create_order_with_access([(pid, 2)])
    # Оплату учитывает платёжная функция (роль worker в рабочей конфигурации; здесь — владелец).
    _owner_value(shop, "INSERT INTO payments (order_id, provider, idempotence_key, amount_minor, currency) "
                       "VALUES (%s, 'yookassa', gen_random_uuid(), 20000, 'RUB')", (order["id"],))
    _owner_value(shop, "UPDATE payments SET provider_payment_id = 'pp1', status = 'pending' WHERE order_id = %s",
                 (order["id"],))
    pay_id = _owner_value(shop, "SELECT id FROM payments WHERE order_id = %s", (order["id"],))
    shop["worker"].run("SELECT payment_record_provider_state(%s, 'pp1', 'succeeded', 20000, 'RUB', TRUE, TRUE, NULL)",
                       (pay_id,))

    as_role(shop["worker"])
    assert run_worker()["fulfilled"] == 1

    as_role(shop["app"])
    body = get_delivery(order["id"], token, keyring_loader=delivery_keyring)
    keys = body["items"][0]["digital_keys"]
    assert len(keys) == 2 and len(set(keys)) == 2 and set(keys) <= set(codes)

    code, out = _cli(shop["admin"], monkeypatch, "stock", "--product", str(pid))
    assert code == 0 and f"{pid} K | digital_keys | 1 | 1 | 2 | 0 | 0 | 0" in out, out


# ---------- R8: кто может вызывать административные функции ----------

@pytest.mark.parametrize("sql", ADMIN_CALLS)
def test_app_and_worker_cannot_call_admin_functions(shop, sql):
    shop["app"].refused(sql)
    shop["worker"].refused(sql)


def test_owner_migrator_is_refused_even_with_execute(shop):
    """Владелец таблиц магазина (мигратор, НЕ суперпользователь) получает отказ, даже имея EXECUTE.

    Если тесты идут под суперпользователем, временная схема принадлежит ему и сработала бы более ранняя
    проверка «суперпользователь». Тогда владельцем digital_keys делается временная LOGIN-роль без прав
    суперпользователя — проверяется именно отказ владельцу."""
    conn = shop["owner"]
    with conn.cursor() as cur:
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        superuser = cur.fetchone()[0]
    conn.commit()
    if not superuser:
        with pytest.raises(Exception, match="под владельцем схемы запрещены"):
            _run(conn, "SELECT admin_whoami()")
        conn.rollback()
        return
    owner = _new_role(shop, "owner")
    s = shop["schema"]
    _run(conn, f"ALTER TABLE {s}.digital_keys OWNER TO {owner.name}")
    _run(conn, f"GRANT USAGE ON SCHEMA {s} TO {owner.name}")
    _run(conn, f"GRANT EXECUTE ON FUNCTION {s}.admin_whoami() TO {owner.name}")
    group = f"{shop['app'].name.rsplit('_', 1)[0]}_admin"
    _run(conn, f"GRANT {group} TO {owner.name}")          # даже член группы администраторов
    owner.refused("SELECT admin_whoami()", match="под владельцем схемы запрещены")


def test_member_with_inherit_is_allowed_and_logged_by_login_name(shop):
    assert shop["admin"].run("SELECT admin_whoami()") == [(shop["admin"].name,)]


def test_rogue_role_with_explicit_execute_but_no_membership_is_refused(shop):
    rogue = _new_role(shop, "rogue")
    s = shop["schema"]
    _run(shop["owner"], f"GRANT USAGE ON SCHEMA {s} TO {rogue.name}")
    _run(shop["owner"], f"GRANT EXECUTE ON FUNCTION {s}.admin_whoami() TO {rogue.name}")
    _run(shop["owner"], f"GRANT EXECUTE ON FUNCTION {s}.admin_reconcile_digital_stock(integer) TO {rogue.name}")
    rogue.refused("SELECT admin_whoami()", match="учётная запись администратора")
    rogue.refused("SELECT admin_reconcile_digital_stock(1)", match="учётная запись администратора")


def test_execute_granted_to_public_does_not_make_admin(shop):
    rogue = _new_role(shop, "nobody")
    s = shop["schema"]
    _run(shop["owner"], f"GRANT USAGE ON SCHEMA {s} TO PUBLIC")
    _run(shop["owner"], f"GRANT EXECUTE ON FUNCTION {s}.admin_whoami() TO PUBLIC")
    rogue.refused("SELECT admin_whoami()", match="учётная запись администратора")
    shop["app"].refused("SELECT admin_whoami()", match="учётная запись администратора")


def test_member_without_inherit_using_set_role_is_refused(shop):
    """SET ROLE в группу даёт EXECUTE (current_user), но вошедший (session_user) прав не наследует."""
    sneaky = _new_role(shop, "sneaky")
    group = f"{shop['app'].name.rsplit('_', 1)[0]}_admin"
    _run(shop["owner"], f"GRANT {group} TO {sneaky.name} WITH INHERIT FALSE, SET TRUE")
    conn = sneaky.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET ROLE {group}")
            with pytest.raises(Exception, match="учётная запись администратора"):
                cur.execute("SELECT admin_whoami()")
        conn.rollback()
    finally:
        conn.close()


def test_noinherit_login_member_is_refused(shop):
    noinh = _new_role(shop, "noinh", "LOGIN NOINHERIT")
    group = f"{shop['app'].name.rsplit('_', 1)[0]}_admin"
    _run(shop["owner"], f"GRANT {group} TO {noinh.name}")
    _run(shop["owner"], f"GRANT USAGE ON SCHEMA {shop['schema']} TO {noinh.name}")
    noinh.refused("SELECT admin_whoami()")   # без наследования нет EXECUTE
    _run(shop["owner"], f"GRANT EXECUTE ON FUNCTION {shop['schema']}.admin_whoami() TO {noinh.name}")
    noinh.refused("SELECT admin_whoami()", match="учётная запись администратора")   # и прав группы нет


def test_set_role_by_real_admin_does_not_change_actor(shop):
    """Администратор, сделавший SET ROLE в другую роль, остаётся собой в журнале (session_user)."""
    group = f"{shop['app'].name.rsplit('_', 1)[0]}_admin"
    conn = shop["admin"].connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET ROLE {group}")
            cur.execute("SELECT admin_whoami()")
            assert cur.fetchone()[0] == shop["admin"].name
        conn.rollback()
    finally:
        conn.close()


def test_admin_group_must_be_configured_and_nologin(shop):
    s = shop["schema"]
    _run(shop["owner"], f"UPDATE {s}.shop_security_settings SET admin_group = '{shop['app'].name}'")
    shop["admin"].refused("SELECT admin_whoami()", match="не настроена")
    _run(shop["owner"], f"DELETE FROM {s}.shop_security_settings")
    shop["admin"].refused("SELECT admin_whoami()", match="не настроена")


def test_superuser_is_refused(shop):
    superuser = os.getenv("DS_TEST_SUPERUSER")
    if not superuser:
        pytest.skip("задайте DS_TEST_SUPERUSER (и при необходимости DS_TEST_SUPERUSER_PASSWORD)")
    role = _Role(superuser, os.getenv("DS_TEST_SUPERUSER_PASSWORD"), shop["schema"])
    role.refused("SELECT admin_whoami()", match="суперпользовател")


def test_cli_refuses_non_admin_logins(shop, monkeypatch):
    for role in (shop["app"], shop["worker"]):
        code, _ = _cli(role, monkeypatch, "stock")
        assert code == 1


def test_admin_functions_are_not_executable_by_public(shop):
    rows = _owner_value(shop, """
        SELECT string_agg(p.proname, ',') FROM pg_proc p
        WHERE p.pronamespace = %s::regnamespace AND p.prosecdef
          AND has_function_privilege('public', p.oid, 'EXECUTE')""", (shop["schema"],))
    assert rows is None


# ---------- прямой доступ к таблицам ----------

@pytest.mark.parametrize("sql", [
    "SELECT * FROM digital_keys",
    "SELECT * FROM digital_key_batches",
    "SELECT * FROM encryption_key_registry",
    "SELECT * FROM digital_key_stock",
    "SELECT * FROM fulfillment_issues",
    "SELECT * FROM shop_security_settings",
    "SELECT digital_keys_assign(1)",
    "SELECT digital_keys_import_core(1, 'x', NULL, NULL, NULL, NULL, NULL, 'x', FALSE, 'x')",
    "SELECT digital_key_stock_counts(1)",
    "SELECT apply_paid_order(1)",
    "INSERT INTO shop_security_settings (admin_group) VALUES ('x')",
])
def test_app_cannot_reach_keys_or_internal_functions(shop, sql):
    shop["app"].refused(sql)


@pytest.mark.parametrize("sql", [
    "SELECT * FROM digital_keys",
    "SELECT ciphertext FROM digital_keys",
    "UPDATE digital_keys SET status = 'available'",
    "SELECT order_delivery_view(1, '\\x00'::bytea)",
    "SELECT digital_keys_withdraw_core(gen_random_uuid(), 'x', 'x')",
    "UPDATE fulfillments SET refund_hold_released_at = now()",
    "UPDATE fulfillments SET cancel_reason = 'x'",
    "UPDATE products SET quantity = 100",
])
def test_worker_cannot_touch_keys_directly(shop, sql):
    shop["worker"].refused(sql)


def test_app_reads_only_sellable_stock(shop):
    pid = _product(shop, quantity=3)
    assert shop["app"].run("SELECT sellable_quantity FROM product_sellable_stock WHERE product_id = %s",
                           (pid,)) == [(0,)]


def test_admin_sees_key_accounting_but_not_ciphertexts(shop, monkeypatch, tmp_path):
    _cli(shop["admin"], monkeypatch, "verify-keyring", "--register")
    pid = _product(shop)
    _import(shop, monkeypatch, pid, unique_codes(1), tmp_path)
    assert shop["admin"].run("SELECT status FROM digital_keys WHERE product_id = %s", (pid,)) == [("available",)]
    for column in ("ciphertext", "nonce", "fingerprint"):
        shop["admin"].refused(f"SELECT {column} FROM digital_keys")
    shop["admin"].refused("UPDATE digital_keys SET status = 'withdrawn'")
    shop["admin"].refused("INSERT INTO encryption_key_registry (purpose, key_id, check_value, registered_by) "
                          "VALUES ('encryption', 'x', '\\x00'::bytea, 'x')")


def test_worker_cannot_fulfill_without_keys_or_resume_or_cancel(shop, as_role):
    from payments.access import create_order_with_access
    s = shop["schema"]
    # Ключи загружаются через ядро импорта владельцем (тест прав worker, не импорта).
    for name, value in keyring().check_values().items():
        purpose, key_id = name.split(":", 1)
        _owner_value(shop, "SELECT encryption_key_register_core(%s, %s, %s, 'pytest')", (purpose, key_id, value))
    from commerce.digital_keys import importer
    digital = _product(shop)
    batch = importer.prepare_batch(keyring(), digital, [(1, "W-1"), (2, "W-2")])
    with shop["owner"].cursor() as cur:
        importer.submit(cur, batch, "pytest", core_actor="pytest")
    shop["owner"].commit()
    as_role(shop["app"])
    order, _ = create_order_with_access([(digital, 2)])
    _owner_value(shop, "SELECT apply_paid_order(%s)", (order["id"],))
    fid = _owner_value(shop, "SELECT f.id FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id "
                             "WHERE i.order_id = %s", (order["id"],))
    shop["worker"].refused(f"UPDATE {s}.fulfillments SET status = 'fulfilled' WHERE id = {fid}", match="назначены не все")
    shop["worker"].refused(f"UPDATE {s}.fulfillments SET status = 'cancelled' WHERE id = {fid}",
                           match="только администратор")
    shop["worker"].run(f"UPDATE {s}.fulfillments SET status = 'requires_action' WHERE id = {fid}")
    shop["worker"].refused(f"UPDATE {s}.fulfillments SET status = 'pending' WHERE id = {fid}",
                           match="только администратор")
    # Администратор возобновляет функцией; worker выдаёт.
    assert jsonb(shop["admin"].run("SELECT admin_resume_fulfillment(%s, 'проверено', FALSE)", (fid,))[0][0])[
        "status"] == "pending"
    assert jsonb(shop["worker"].run("SELECT digital_keys_assign(%s)", (fid,))[0][0])["result"] == "assigned"


def test_admin_login_sql_works_with_any_prefix(shop):
    from deploy import db_roles
    prefix = shop["app"].name.rsplit("_", 1)[0]
    login = f"{prefix}_ivan"
    sql = db_roles.admin_login_sql(login, prefix)
    _run(shop["owner"], sql)
    shop.setdefault("extra_roles", []).append(login)
    password = secrets.token_urlsafe(16)
    _run(shop["owner"], f"ALTER ROLE {login} PASSWORD '{password}'")
    ivan = _Role(login, password, shop["schema"])
    assert ivan.run("SELECT admin_whoami()") == [(login,)]
    # SET FALSE: стать группой нельзя.
    ivan.refused(f"SET ROLE {prefix}_admin", match="permission denied|нет прав|разрешения")
    with pytest.raises(ValueError):
        db_roles.admin_login_sql(f"{prefix}_app", prefix)


def test_cli_import_refuses_secret_that_differs_from_registry(shop, monkeypatch, tmp_path):
    """Проверка реестра — в процессе импорта, где секреты есть: другой секрет под тем же id отклоняется
    до шифрования и записи (иначе в базе оказались бы ключи, которые веб-процесс не расшифрует)."""
    import base64
    assert _cli(shop["admin"], monkeypatch, "verify-keyring", "--register")[0] == 0
    pid = _product(shop)
    other = base64.b64encode(b"\x01" * 32).decode()
    path = tmp_path / "k.txt"
    path.write_text("A-1\n", encoding="utf-8")
    for name, value in TEST_ENV.items():
        monkeypatch.setenv(name, value)
    for variable, value in (("DIGITAL_KEYS_FINGERPRINT_KEY", f"tf1:{other}"),
                            ("DIGITAL_KEYS_ENCRYPTION_KEYS", f"t1:{other}")):
        monkeypatch.setenv(variable, value)
        lines = []
        code = admin_cli.main(["import-keys", "--product", str(pid), "--file", str(path), "--label", "x"],
                              connect=shop["admin"].connect, out=lines.append)
        assert code == 1
        code = admin_cli.main(["verify-keyring"], connect=shop["admin"].connect, out=lines.append)
        assert code == 1 and "НЕ СОВПАДАЕТ" in "\n".join(lines)
        monkeypatch.setenv(variable, TEST_ENV[variable])
    assert _owner_value(shop, "SELECT count(*) FROM digital_keys WHERE product_id = %s", (pid,)) == 0
    assert _import(shop, monkeypatch, pid, ["A-1"], tmp_path)[0] == 0


def test_cli_verify_keyring_decrypts_a_sample_of_each_version(shop, monkeypatch, tmp_path):
    assert _cli(shop["admin"], monkeypatch, "verify-keyring", "--register")[0] == 0
    pid = _product(shop)
    _import(shop, monkeypatch, pid, unique_codes(2), tmp_path)
    code, out = _cli(shop["admin"], monkeypatch, "verify-keyring")
    assert code == 0 and "ключей версии t1: 2 — образец расшифрован" in out, out
    # Процесс без версии t1 (например, после потери секрета) — явная ошибка.
    monkeypatch.setenv("DIGITAL_KEYS_ENCRYPTION_KEYS", TEST_ENV["DIGITAL_KEYS_ENCRYPTION_KEYS"].split(",")[1])
    monkeypatch.setenv("DIGITAL_KEYS_ACTIVE_KEY", "t2")
    lines = []
    assert admin_cli.main(["verify-keyring"], connect=shop["admin"].connect, out=lines.append) == 1
    assert "секрета этой версии в процессе НЕТ" in "\n".join(lines)
