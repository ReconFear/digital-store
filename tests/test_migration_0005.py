"""Миграция 0005 (аккаунты покупателей, P1): 0000–0004 не изменены, переход 0004 -> 0005 не меняет
данные PR2–PR5, флаги выключены по умолчанию, SECURITY DEFINER / search_path / lock_timeout / PUBLIC,
ограничения хэшей, базовое поведение функций под владельцем схемы.

Офлайн-тесты (текст миграции, контрольные суммы) к базе не обращаются. Остальные работают во временной
схеме тестовой базы (make_schema) и требуют pgcrypto в схеме ext_pgcrypto с USAGE у тестовой роли
(DB_ROLES.md, шаг 4а). Конкурентность (K-сценарии) и права ролей app/worker/admin — PR6 Stage 1c.
"""

import base64
import hashlib
import os
import re
import threading
import time

import psycopg
import pytest

import migrate
import test_migration_0004
import test_migrations
from test_migration_0003 import _dir_until, _fails
from test_migrations import PROJECT_DIR, _query, _quiet, _run

make_schema = test_migrations.make_schema
at_0003 = test_migration_0004.at_0003

MIGRATION = PROJECT_DIR / "migrations" / "0005_customer_auth.sql"

# Контрольные суммы применённых у пользователя миграций (PR1–PR5). Менять нельзя.
OLD_CHECKSUMS = dict(test_migration_0004.OLD_CHECKSUMS,
                     **{"0004": "b0bda706a5e95aa4e3c48088160791295d5b4ddf378ccc228a33adcad8ae9b08"})

DEFINER_0005 = {
    "auth_register", "auth_login", "auth_password_change", "auth_session_resolve", "auth_session_touch",
    "auth_session_revoke", "auth_sessions_revoke_all", "customer_profile_view", "customer_orders_view",
    "customer_order_view", "customer_order_link_new", "auth_maintenance_purge", "admin_customer_lookup",
    "admin_customer_disable", "admin_customer_revoke_sessions", "admin_customer_release_email",
    "admin_auth_settings_update"}
INVOKER_0005 = {
    "auth_settings_dummy_hash", "customer_sessions_guard", "customer_orders_protect",
    "customer_orders_protect_truncate", "auth_require_read_committed", "auth_check_email", "auth_check_not_reserved",
    "auth_check_proof", "auth_check_proof_pair", "auth_check_token", "customer_session_customer",
    "customer_sessions_invalidate", "auth_session_refusal", "customers_hash_cost_guard",
    "auth_check_reason", "customer_lookup_core", "customer_disable_core",
    "customer_revoke_sessions_core", "customer_release_email_core", "auth_settings_update_core"}
FUNCTIONS_0005 = DEFINER_0005 | INVOKER_0005
# Функции с этапом блокировок (ожидание → 'busy'); у остальных lock_timeout не задаётся.
LOCK_TIMEOUT_0005 = {"auth_register", "auth_login", "auth_password_change", "auth_sessions_revoke_all",
                     "customer_disable_core", "customer_revoke_sessions_core", "customer_release_email_core"}
TABLES_0005 = {"auth_settings", "customers", "customer_sessions", "customer_orders", "auth_throttle"}

EMAIL = "buyer@example.test"
LOGIN = "SELECT auth_login(%s, %s, %s::smallint, %s, %s::smallint, %s, %s)"
REGISTER = "SELECT auth_register(%s, %s, %s::smallint, %s)"
CHANGE = "SELECT auth_password_change(%s, %s, %s::smallint, %s, %s::smallint, %s, %s::smallint, %s)"


def _proof():
    """Синтетическое доказательство пароля: 43 символа base64url (перец знает только приложение)."""
    return base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()


def _token():
    return os.urandom(32)


def _call(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


def _sql_without_comments(text):
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


# ---------- офлайн: файлы миграций ----------

def test_old_migration_checksums_are_unchanged():
    found = {m.version: m.checksum for m in migrate.discover() if m.version in OLD_CHECKSUMS}
    assert found == OLD_CHECKSUMS


def test_0005_is_last_and_checksummed():
    versions = [m.version for m in migrate.discover()]
    assert versions == ["0000", "0001", "0002", "0003", "0004", "0005"]
    m = migrate.discover()[-1]
    assert m.path.name == "0005_customer_auth.sql"
    assert m.checksum == hashlib.sha256(migrate._normalize(m.path.read_bytes()).encode("utf-8")).hexdigest()


def test_schema_sql_ends_with_0005():
    text = (PROJECT_DIR / "schema.sql").read_text(encoding="utf-8-sig")
    assert "CREATE TABLE customer_sessions" in text and "customer_order_link_new" in text
    assert text.rstrip().endswith(MIGRATION.read_text(encoding="utf-8-sig").rstrip())


def test_every_function_is_pinned_and_revoked_in_the_final_block():
    text = MIGRATION.read_text(encoding="utf-8-sig")
    created = set(re.findall(r"^CREATE FUNCTION ([a-z_]+)\(", text, flags=re.MULTILINE))
    block = text[text.rindex("expected CONSTANT TEXT[]"):]
    listed = set(re.findall(r"'([a-z_]+)'", block[:block.index("];")]))
    assert created == listed == FUNCTIONS_0005
    assert "ALTER FUNCTION %s SET search_path = pg_catalog, %I, pg_temp" in block
    assert "REVOKE ALL ON FUNCTION %s FROM PUBLIC" in block


def test_migration_text_rules():
    sql = _sql_without_comments(MIGRATION.read_text(encoding="utf-8-sig"))
    # Таблицы — без жёсткой схемы public: закреплённый search_path, как в 0003/0004 (решение Q1).
    assert re.search(r"\bpublic\.", sql) is None
    # pgcrypto — только квалифицированные вызовы, только эти функции.
    assert set(re.findall(r"ext_pgcrypto\.([a-z_]+)", sql)) == {"crypt", "gen_salt", "gen_random_bytes"}
    assert re.search(r"(?<![.\w])(crypt|gen_salt|gen_random_bytes)\(", sql) is None
    # Права выдаёт deploy/db_roles.py (Stage 1c), не миграция; динамический SQL — только в блоке закрепления.
    assert re.search(r"\bGRANT\b", sql) is None
    assert sql.count("EXECUTE format(") == 2
    # Флаги выключены по умолчанию.
    assert "accounts_enabled BOOLEAN NOT NULL DEFAULT FALSE" in sql
    assert "registration_open BOOLEAN NOT NULL DEFAULT FALSE" in sql
    # Ни одного объекта отвергнутой архитектуры A2.
    for a2 in ("auth_login_attempts", "xid_begin", "pg_current_xact_id", "auth_login_finish", "auth_login_begin"):
        assert a2 not in sql
    # Привязка заказа: FOR SHARE на покупателе, затем отдельный оператор проверки сессии.
    link = sql[sql.index("CREATE FUNCTION customer_order_link_new"):]
    link = link[:link.index("$$;")]
    share = link.index("FOR SHARE")
    recheck = link[share:]
    assert "s.auth_epoch = c.auth_epoch" in recheck
    # Повторная проверка после ожидания включает флаг аккаунтов и сроки на момент проверки.
    assert "accounts_enabled" in recheck and "clock_timestamp()" in recheck and "now()" not in recheck
    assert "FOR SHARE" not in link[share + 1:] and "lock_timeout" not in link
    # Сроки сессий проверяются по clock_timestamp(); now() в проверках действительности не используется.
    helper = sql[sql.index("CREATE FUNCTION customer_session_customer"):]
    helper = helper[:helper.index("$$;")]
    assert "clock_timestamp()" in helper and "now()" not in helper and "accounts_enabled" in helper
    for fn in ("auth_login", "auth_password_change"):
        body = sql[sql.index(f"CREATE FUNCTION {fn}("):]
        body = body[:body.index("$$;")]
        assert "FOR NO KEY UPDATE" in body and "FOR SHARE" not in body and "FOR UPDATE" not in body.replace(
            "FOR NO KEY UPDATE", "")
        # Все блокировки — до bcrypt: первый вызов crypt идёт после обработчика lock_not_available.
        assert body.index("EXCEPTION WHEN lock_not_available") < body.index("ext_pgcrypto.crypt")


def test_all_sha256_columns_have_length_check():
    sql = _sql_without_comments(MIGRATION.read_text(encoding="utf-8-sig"))
    columns = re.findall(r"^\s+([a-z_]*sha256) BYTEA", sql, flags=re.MULTILINE)
    assert set(columns) == {"token_sha256", "key_sha256"}
    for column in columns:
        assert f"CHECK (octet_length({column}) = 32)" in sql


# ---------- с тестовой базой ----------

@pytest.fixture
def at_0005(make_schema):
    """Схема со всеми миграциями; bcrypt cost 4 — только для этой изолированной тестовой схемы."""
    conn, schema = make_schema()
    migrate.apply_migrations(conn, log=_quiet)
    _run(conn, "UPDATE auth_settings SET bcrypt_cost = 4")
    return conn, schema


@pytest.fixture
def accounts(at_0005):
    conn, schema = at_0005
    _run(conn, "UPDATE auth_settings SET accounts_enabled = TRUE, registration_open = TRUE")
    return conn, schema


def _register(conn, email=EMAIL, proof=None, ver=1):
    proof, session = proof or _proof(), _token()
    assert _call(conn, REGISTER, (email, proof, ver, session)) == {"result": "created"}
    return proof, session


def _login(conn, proof, ver=1, email=EMAIL, prev=(None, None), prev_session=None):
    session = _token()
    return _call(conn, LOGIN, (email, proof, ver, prev[0], prev[1], session, prev_session)), session


def _valid(conn, session):
    return _call(conn, "SELECT auth_session_resolve(%s)", (session,))["valid"]


def test_upgrade_from_0004_applies_only_0005_and_keeps_data(at_0003, tmp_path):
    conn, _, _ = at_0003
    migrate.apply_migrations(conn, _dir_until(tmp_path, "0004"), log=_quiet)
    tables = dict(test_migration_0004.SNAPSHOT_SQL,
                  fulfillments="SELECT * FROM fulfillments ORDER BY id",
                  digital_keys="SELECT * FROM digital_keys ORDER BY id",
                  shop_security_settings="SELECT * FROM shop_security_settings")
    before = {name: _query(conn, sql) for name, sql in tables.items()}
    journal_before = _query(conn, "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version")

    applied = migrate.apply_migrations(conn, log=_quiet)

    assert [m.version for m in applied] == ["0005"]
    assert {name: _query(conn, sql) for name, sql in tables.items()} == before
    assert _query(conn, "SELECT version, checksum, applied_at FROM schema_migrations ORDER BY version")[:5] \
        == journal_before
    for table in ("customers", "customer_sessions", "customer_orders", "auth_throttle"):
        assert _query(conn, f"SELECT count(*) FROM {table}") == [(0,)]
    assert migrate.apply_migrations(conn, log=_quiet) == []


def test_defaults_accounts_and_registration_disabled(make_schema):
    conn, _ = make_schema()
    migrate.apply_migrations(conn, log=_quiet)
    row = _query(conn, "SELECT accounts_enabled, registration_open, login_window, login_limit, bcrypt_cost, "
                       "left(dummy_hash, 7) FROM auth_settings")
    assert len(row) == 1
    enabled, registration, window, limit, cost, prefix = row[0]
    assert (enabled, registration, limit, cost, prefix) == (False, False, 10, 12, "$2a$12$")
    assert window.total_seconds() == 15 * 60


def test_disabled_flags_refuse_everything_without_bcrypt(at_0005):
    conn, _ = at_0005
    proof, session = _proof(), _token()
    assert _call(conn, REGISTER, (EMAIL, proof, 1, session)) == {"result": "disabled"}
    assert _call(conn, LOGIN, (EMAIL, proof, 1, None, None, session, None)) == {"result": "disabled"}
    assert _call(conn, CHANGE, (session, proof, 1, None, None, _proof(), 1, _token())) == {"result": "disabled"}
    assert _valid(conn, session) is False
    assert _call(conn, "SELECT customer_profile_view(%s)", (session,)) is None
    # Счётчики не тронуты: отказ — до этапа блокировок и bcrypt.
    assert _query(conn, "SELECT count(*) FROM auth_throttle") == [(0,)]


def test_registration_closed_but_login_works(accounts):
    conn, _ = accounts
    proof, _ = _register(conn)
    _run(conn, "UPDATE auth_settings SET registration_open = FALSE")
    assert _call(conn, REGISTER, ("other@example.test", _proof(), 1, _token())) == {"result": "closed"}
    assert _login(conn, proof)[0] == {"result": "ok"}


def test_register_login_resolve(accounts):
    conn, _ = accounts
    proof, reg_session = _register(conn)
    assert _valid(conn, reg_session) is True
    assert _call(conn, REGISTER, (EMAIL, _proof(), 1, _token())) == {"result": "email_taken"}

    result, session = _login(conn, proof, prev_session=reg_session)
    assert result == {"result": "ok"}
    assert _valid(conn, session) is True
    assert _valid(conn, reg_session) is False             # прежняя сессия того же покупателя отозвана
    assert _login(conn, _proof())[0] == {"result": "invalid"}
    assert _login(conn, proof, email="nobody@example.test")[0] == {"result": "invalid"}
    # Секреты не возвращаются и не попадают в аудит.
    assert _call(conn, "SELECT customer_profile_view(%s)", (session,))["email"] == EMAIL
    assert _query(conn, "SELECT count(*) FROM audit_log WHERE details::text LIKE '%%example.test%%' "
                        "OR details::text LIKE %s", (f"%{proof}%",)) == [(0,)]


def test_input_format_violations_raise(accounts):
    conn, _ = accounts
    good = _proof()
    for proof in (good[:42], good + "A", good[:42] + "="):
        _fails(conn, LOGIN, "доказательства", (EMAIL, proof, 1, None, None, _token(), None))
    _fails(conn, LOGIN, "предыдущая версия", (EMAIL, good, 1, _proof(), 1, _token(), None))
    _fails(conn, LOGIN, "email_norm", ("Buyer@Example.test", good, 1, None, None, _token(), None))
    _fails(conn, LOGIN, "32 байта", (EMAIL, good, 1, None, None, os.urandom(31), None))
    _fails(conn, REGISTER, "invalid", ("released+1@invalid", good, 1, _token()))
    _fails(conn, LOGIN, "invalid", ("released+1@invalid", good, 1, None, None, _token(), None))


def test_repeatable_read_is_refused(accounts):
    conn, _ = accounts
    with pytest.raises(psycopg.errors.RaiseException, match="READ COMMITTED"):
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cur.execute(LOGIN, (EMAIL, _proof(), 1, None, None, _token(), None))
    conn.rollback()


def test_login_throttle_window_and_bounded_audit(accounts):
    conn, _ = accounts
    proof, _ = _register(conn)
    _run(conn, "UPDATE auth_settings SET login_limit = 3")
    for _ in range(3):
        assert _login(conn, _proof())[0] == {"result": "invalid"}
    for _ in range(3):                                    # и верный пароль — без проверки
        result = _login(conn, proof)[0]
        assert result["result"] == "throttled" and result["retry_after_seconds"] >= 1
    assert _query(conn, "SELECT attempts FROM auth_throttle WHERE scope = 1") == [(6,)]
    assert _query(conn, "SELECT count(*) FROM audit_log WHERE action = 'auth.login_throttled'") == [(1,)]
    # Неверные попытки и неизвестные email в аудит не пишутся.
    for _ in range(5):
        _login(conn, _proof(), email="ghost@example.test")
    assert _query(conn, "SELECT count(*) FROM audit_log WHERE action NOT IN "
                        "('auth.register', 'auth.login_throttled')") == [(0,)]
    # Новое окно открывается после истечения прежнего; запросы во время блокировки его не сдвигают.
    _run(conn, "UPDATE auth_throttle SET window_start = window_start - INTERVAL '16 minutes'")
    assert _login(conn, proof)[0] == {"result": "ok"}
    assert _query(conn, "SELECT attempts FROM auth_throttle WHERE scope = 1 AND attempts > 0") == [(5,)]


def test_pepper_rotation_rehash_and_missing_version(accounts):
    conn, _ = accounts
    old_proof, _ = _register(conn, ver=1)
    new_proof = _proof()                                  # то же пароль, перец версии 2
    epoch = _query(conn, "SELECT auth_epoch FROM customers")[0][0]
    assert _login(conn, new_proof, ver=2, prev=(old_proof, 1))[0] == {"result": "ok"}
    assert _query(conn, "SELECT pepper_version, auth_epoch FROM customers") == [(2, epoch)]
    assert _login(conn, new_proof, ver=2)[0] == {"result": "ok"}
    # Нужной версии среди переданных нет → invalid и одна запись аудита за окно.
    for _ in range(3):
        assert _login(conn, _proof(), ver=3)[0] == {"result": "invalid"}
    assert _query(conn, "SELECT count(*) FROM audit_log WHERE action = 'auth.pepper_missing'") == [(1,)]


def test_password_change_bumps_epoch_and_revokes_sessions(accounts):
    conn, _ = accounts
    proof, _ = _register(conn)
    _, s1 = _login(conn, proof)
    _, s2 = _login(conn, proof)
    new_proof, new_session = _proof(), _token()
    epoch = _query(conn, "SELECT auth_epoch FROM customers")[0][0]

    assert _call(conn, CHANGE, (s1, _proof(), 1, None, None, new_proof, 1, _token())) == {"result": "invalid"}
    assert _call(conn, CHANGE, (s1, proof, 1, None, None, new_proof, 1, new_session)) == {"result": "ok"}

    assert _query(conn, "SELECT auth_epoch FROM customers") == [(epoch + 1,)]
    assert (_valid(conn, s1), _valid(conn, s2), _valid(conn, new_session)) == (False, False, True)
    assert _login(conn, proof)[0] == {"result": "invalid"}
    assert _login(conn, new_proof)[0] == {"result": "ok"}
    assert _call(conn, CHANGE, (s1, new_proof, 1, None, None, _proof(), 1, _token())) == \
        {"result": "session_invalid"}


def test_revoke_all_disable_and_release_email(accounts):
    conn, _ = accounts
    proof, session = _register(conn)
    assert _call(conn, "SELECT auth_sessions_revoke_all(%s)", (session,)) == {"result": "ok"}
    assert _valid(conn, session) is False
    cid = _query(conn, "SELECT id FROM customers")[0][0]

    _, session = _login(conn, proof)
    assert _call(conn, "SELECT customer_revoke_sessions_core(%s, 'тест', 'pytest')", (cid,)) == {"result": "ok"}
    assert _valid(conn, session) is False

    _, session = _login(conn, proof)
    assert _call(conn, "SELECT customer_release_email_core(%s, 'тест', 'pytest')", (cid,)) == {"result": "ok"}
    assert _valid(conn, session) is False
    assert _query(conn, "SELECT email_norm FROM customers WHERE id = %s", (cid,)) == [(f"released+{cid}@invalid",)]
    assert _call(conn, "SELECT customer_release_email_core(%s, 'тест', 'pytest')", (cid,)) == \
        {"result": "already_released"}
    new_proof, _ = _register(conn)                        # адрес снова свободен

    new_cid = _query(conn, "SELECT id FROM customers WHERE email_norm = %s", (EMAIL,))[0][0]
    assert _login(conn, new_proof)[0] == {"result": "ok"}
    assert _call(conn, "SELECT customer_disable_core(%s, 'тест', 'pytest')", (new_cid,)) == {"result": "ok"}
    assert _login(conn, new_proof)[0] == {"result": "invalid"}   # отключён: как неверный пароль
    assert _call(conn, "SELECT customer_disable_core(%s, 'тест', 'pytest')", (10 ** 9,)) == {"result": "not_found"}
    _fails(conn, "SELECT customer_disable_core(1, ' ', 'pytest')", "причину")
    # Аудит администрирования — без email.
    assert _query(conn, "SELECT count(*) FROM audit_log WHERE action LIKE 'admin.%%' "
                        "AND details::text LIKE '%%example.test%%'") == [(0,)]


def test_admin_wrappers_refuse_schema_owner(accounts):
    conn, _ = accounts
    for sql in ("SELECT admin_customer_lookup('a@example.test')", "SELECT admin_customer_disable(1, 'x')",
                "SELECT admin_customer_revoke_sessions(1, 'x')", "SELECT admin_customer_release_email(1, 'x')",
                "SELECT admin_auth_settings_update(TRUE, TRUE)"):
        _fails(conn, sql, "группа администраторов|суперпользовател|владельцем")


@pytest.fixture
def product(accounts):
    conn, _ = accounts
    return _call(conn, "INSERT INTO products (name, price_minor, currency, quantity, is_for_sale) "
                       "VALUES ('A', 10000, 'RUB', 50, TRUE) RETURNING id")


def _new_order(cur, product_id, token_sha256=None, status="pending", access=True):
    """Заказ v2 в текущей транзакции, как в PR2/PR3: заказ, позиция, гостевой секрет (order_access_issue)."""
    cur.execute("INSERT INTO orders (status, currency, total_minor) VALUES (%s, 'RUB', 10000) RETURNING id",
                (status,))
    order_id = cur.fetchone()[0]
    cur.execute("INSERT INTO order_items (order_id, product_id, product_name, product_type, fulfillment_type, "
                "quantity, unit_price_minor, currency, line_total_minor) "
                "VALUES (%s, %s, 'A', 'other', 'manual', 1, 10000, 'RUB', 10000)", (order_id, product_id))
    if access:
        cur.execute("SELECT order_access_issue(%s, %s)",
                    (order_id, token_sha256 or hashlib.sha256(os.urandom(32)).digest()))
    return order_id


def _link(cur, session, order_id):
    cur.execute("SELECT customer_order_link_new(%s, %s)", (session, order_id))
    return cur.fetchone()[0]


def test_link_only_new_order_of_this_transaction(accounts, product):
    conn, _ = accounts
    _, session = _register(conn)
    with conn.cursor() as cur:
        order_id = _new_order(cur, product)
        assert _link(cur, session, order_id) == {"result": "linked"}
    conn.commit()
    assert _query(conn, "SELECT items_sealed FROM orders WHERE id = %s", (order_id,)) == [(True,)]
    assert _query(conn, "SELECT count(*) FROM order_access WHERE order_id = %s", (order_id,)) == [(1,)]

    # Зафиксированный (запечатанный) заказ и повторная привязка — отказ.
    _fails(conn, "SELECT customer_order_link_new(%s, %s)", "только новый заказ", (session, order_id))
    # Заказ без гостевого секрета — отказ.
    with pytest.raises(psycopg.errors.RaiseException, match="секрет доступа"):
        with conn.cursor() as cur:
            cur.execute("INSERT INTO orders (status, currency, total_minor) VALUES ('pending', 'RUB', 10000) "
                        "RETURNING id")
            _link(cur, session, cur.fetchone()[0])
    conn.rollback()


def test_link_refused_for_invalid_session_and_views_hide_foreign_orders(accounts, product):
    conn, _ = accounts
    _, owner_session = _register(conn)
    _, other_session = _register(conn, email="other@example.test")
    with conn.cursor() as cur:
        order_id = _new_order(cur, product)
        _link(cur, owner_session, order_id)
    conn.commit()

    own = _call(conn, "SELECT customer_order_view(%s, %s)", (owner_session, order_id))
    assert own["order"]["id"] == order_id and len(own["items"]) == 1
    assert "token" not in str(own)
    assert _call(conn, "SELECT customer_order_view(%s, %s)", (other_session, order_id)) is None
    assert _call(conn, "SELECT customer_order_view(%s, %s)", (other_session, 10 ** 9)) is None
    assert _call(conn, "SELECT customer_orders_view(%s)", (other_session,)) == []
    assert [o["id"] for o in _call(conn, "SELECT customer_orders_view(%s)", (owner_session,))] == [order_id]

    _call(conn, "SELECT auth_session_revoke(%s)", (other_session,))
    with conn.cursor() as cur:
        foreign = _new_order(cur, product)
        assert _link(cur, other_session, foreign) == {"result": "session_invalid"}
    conn.rollback()


def test_session_touch_never_revives(accounts):
    conn, _ = accounts
    _, session = _register(conn)
    _run(conn, "UPDATE auth_settings SET session_touch_interval = INTERVAL '0'")
    assert _call(conn, "SELECT auth_session_touch(%s)", (session,)) == {"touched": True}
    _call(conn, "SELECT auth_session_revoke(%s)", (session,))
    assert _call(conn, "SELECT auth_session_touch(%s)", (session,)) == {"touched": False}
    _fails(conn, "UPDATE customer_sessions SET revoked_at = NULL", "отозванная")
    _fails(conn, "UPDATE customer_sessions SET last_seen_at = now()", "отозванная")


def test_session_identity_and_order_links_are_immutable(accounts, product):
    conn, _ = accounts
    _, session = _register(conn)
    with conn.cursor() as cur:
        _link(cur, session, _new_order(cur, product))
    conn.commit()
    _fails(conn, "UPDATE customer_sessions SET auth_epoch = auth_epoch + 1", "неизменяемы")
    _fails(conn, "UPDATE customer_sessions SET expires_at = expires_at + INTERVAL '1 day'", "неизменяемы")
    _fails(conn, "UPDATE customer_orders SET customer_id = customer_id", "неизменяема")
    _fails(conn, "DELETE FROM customer_orders", "неизменяема")
    _fails(conn, "TRUNCATE customer_orders", "TRUNCATE")


def test_hash_length_checks(accounts):
    conn, _ = accounts
    _register(conn)
    cid = _query(conn, "SELECT id FROM customers")[0][0]
    for size in (31, 33):
        _fails(conn, "INSERT INTO auth_throttle (scope, key_sha256, window_start, attempts) VALUES (1, %s, now(), 1)",
               "auth_throttle_key_sha256_check", (os.urandom(size),))
        _fails(conn, "INSERT INTO customer_sessions (customer_id, token_sha256, auth_epoch, idle_expires_at, "
                     "expires_at) VALUES (%s, %s, 1, now() + INTERVAL '1 day', now() + INTERVAL '2 days')",
               "customer_sessions_token_sha256_check", (cid, os.urandom(size)))


def test_bcrypt_cost_and_dummy_hash_change_together(at_0005):
    conn, _ = at_0005
    assert _query(conn, "SELECT left(dummy_hash, 7) FROM auth_settings") == [("$2a$04$",)]
    _run(conn, "UPDATE auth_settings SET bcrypt_cost = 5")
    assert _query(conn, "SELECT left(dummy_hash, 7) FROM auth_settings") == [("$2a$05$",)]
    # Ручная подмена фиктивного хэша заменяется хэшем текущей стоимости.
    _run(conn, "UPDATE auth_settings SET dummy_hash = repeat('x', 60)")
    assert _query(conn, "SELECT left(dummy_hash, 7) FROM auth_settings") == [("$2a$05$",)]
    for cost in (3, 16):
        _fails(conn, f"UPDATE auth_settings SET bcrypt_cost = {cost}", "4..15")
    assert _query(conn, "SELECT bcrypt_cost FROM auth_settings") == [(5,)]


def test_purge_removes_expired_rows(accounts):
    conn, _ = accounts
    _register(conn)
    cid = _query(conn, "SELECT id FROM customers")[0][0]
    _run(conn, "INSERT INTO auth_throttle (scope, key_sha256, window_start, attempts) "
               f"VALUES (1, decode(repeat('ab', 32), 'hex'), now() - INTERVAL '1 hour', 3)")
    _run(conn, "INSERT INTO customer_sessions (customer_id, token_sha256, auth_epoch, created_at, idle_expires_at, "
               f"expires_at) VALUES ({cid}, decode(repeat('cd', 32), 'hex'), 1, now() - INTERVAL '100 days', "
               "now() - INTERVAL '10 days', now() - INTERVAL '2 days')")
    assert _call(conn, "SELECT auth_maintenance_purge()") == {"throttle_deleted": 1, "sessions_deleted": 1}
    assert _query(conn, "SELECT count(*) FROM customer_sessions") == [(1,)]   # сессия регистрации жива


def test_security_definer_search_path_lock_timeout_and_public_execute(at_0005):
    conn, schema = at_0005
    rows = _query(conn, """
        SELECT p.proname, p.prosecdef, coalesce(array_to_string(p.proacl, ','), ''), p.proconfig
        FROM pg_proc p WHERE p.pronamespace = %s::regnamespace""", (schema,))
    by_name = {name: (definer, acl, config or []) for name, definer, acl, config in rows}
    assert FUNCTIONS_0005 <= set(by_name)
    assert {n for n in FUNCTIONS_0005 if by_name[n][0]} == DEFINER_0005
    expected_path = f"search_path=pg_catalog, {schema}, pg_temp"
    for name, (_, acl, config) in by_name.items():
        paths = [c for c in config if c.startswith("search_path=")]
        assert paths == [expected_path], name
        others = sorted(c for c in config if not c.startswith("search_path="))
        if name in LOCK_TIMEOUT_0005:
            assert others == ["lock_timeout=2s"], name
        else:
            assert others == [], name                     # в том числе у функций 0000–0004 ничего не добавилось
        if name in FUNCTIONS_0005:
            assert acl and not any(entry.startswith("=") for entry in acl.split(",")), (name, acl)


def test_tables_have_no_privileges_for_public(at_0005):
    conn, schema = at_0005
    rows = _query(conn, """
        SELECT c.relname FROM pg_class c, aclexplode(coalesce(c.relacl, acldefault('r', c.relowner))) a
        WHERE c.relnamespace = %s::regnamespace AND a.grantee = 0 AND c.relname::text = ANY(%s)""",
                  (schema, list(TABLES_0005)))
    assert rows == []


def test_temp_objects_do_not_shadow_auth_functions(at_0005):
    """Закреплённый search_path (pg_temp последним): временные таблицы и функции атакующего с тем же
    именем не влияют на результат (решение Q1, D2)."""
    import database
    conn, schema = at_0005                                  # аккаунты выключены в настоящей таблице
    session = _token()
    attacker = database.get_connection()
    try:
        with attacker.cursor() as cur:
            cur.execute(f"SET search_path TO pg_temp, {schema}")
            cur.execute("CREATE TEMP TABLE auth_settings (singleton BOOLEAN, accounts_enabled BOOLEAN)")
            cur.execute("INSERT INTO pg_temp.auth_settings VALUES (TRUE, TRUE)")
            cur.execute("CREATE TEMP TABLE customer_sessions (token_sha256 BYTEA, customer_id BIGINT)")
            cur.execute("INSERT INTO pg_temp.customer_sessions VALUES (%s, 1)", (session,))
            cur.execute("CREATE FUNCTION pg_temp.crypt(TEXT, TEXT) RETURNS TEXT LANGUAGE sql AS 'SELECT $2'")
            cur.execute(f"SELECT {schema}.auth_login(%s, %s, 1::smallint, NULL, NULL, %s, NULL)",
                        (EMAIL, _proof(), _token()))
            assert cur.fetchone()[0] == {"result": "disabled"}
            cur.execute(f"SELECT {schema}.auth_session_resolve(%s)", (session,))
            assert cur.fetchone()[0] == {"valid": False}
        attacker.rollback()
    finally:
        attacker.close()


# ---------- несколько соединений (аудит Stage 1b: флаг, новый заказ, стоимость bcrypt, время) ----------
#
# Как в tests/test_digital_keys_concurrency.py: соединение держит транзакцию открытой, другое в потоке
# выполняет конфликтующий шаг, ожидание подтверждается по pg_stat_activity / pg_blocking_pids.

def _exec(conn, sql, params=None):
    """Без COMMIT: транзакция остаётся открытой."""
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0] if cur.description else None


class _Background:
    def __init__(self, fn):
        self.result = self.error = None
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def _run(self, fn):
        try:
            self.result = fn()
        except BaseException as exc:  # noqa: BLE001
            self.error = exc

    def join(self, timeout=15):
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "шаг в потоке не завершился"
        return self


def _wait_blocked(observer, pid, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = _query(observer, "SELECT wait_event_type, cardinality(pg_blocking_pids(%s)) FROM pg_stat_activity "
                                "WHERE pid = %s", (pid, pid))
        if rows and (rows[0][0] == "Lock" or rows[0][1]):
            return
        time.sleep(0.01)
    raise AssertionError(f"соединение {pid} не встало на ожидание блокировки")


@pytest.fixture
def peers(accounts):
    """peers() -> (соединение, pid) в той же временной схеме; закрываются до удаления схемы."""
    import database
    _, schema = accounts
    opened = []

    def connect():
        conn = database.get_connection()
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("SET lock_timeout = '5s'")
            cur.execute("SELECT pg_backend_pid()")
            pid = cur.fetchone()[0]
        conn.commit()
        opened.append(conn)
        return conn, pid

    yield connect
    for conn in opened:
        try:
            conn.rollback()
        finally:
            conn.close()


def _customer_id(conn, email=EMAIL):
    return _query(conn, "SELECT id FROM customers WHERE email_norm = %s", (email,))[0][0]


def _short_session(conn, email=EMAIL, seconds=1):
    """Сессия, которая истекает через seconds секунд (по часам сервера)."""
    token = _token()
    _call(conn, "INSERT INTO customer_sessions (customer_id, token_sha256, auth_epoch, idle_expires_at, expires_at) "
                "SELECT id, %s, auth_epoch, clock_timestamp() + make_interval(secs => %s), "
                "clock_timestamp() + make_interval(secs => %s) FROM customers WHERE email_norm = %s RETURNING id",
          (token, seconds, seconds, email))
    return token


# 1. Флаг accounts_enabled: точка переключения и линеаризация.

def test_login_in_flight_across_disable_is_linearized_before_it(accounts, peers):
    """Вход прочитал флаг до выключения и зафиксировался после: его сессия ведёт себя как созданная до
    выключения — не действует, пока флаг выключен, и действует после повторного включения. Новый вход
    после выключения — 'disabled' без учёта попытки и без bcrypt."""
    conn, _ = accounts
    proof, _ = _register(conn)
    login, _ = peers()
    session = _token()
    assert _exec(login, LOGIN, (EMAIL, proof, 1, None, None, session, None)) == {"result": "ok"}
    _run(conn, "UPDATE auth_settings SET accounts_enabled = FALSE")     # не ждёт: строку настроек вход не держит
    login.commit()

    assert _valid(conn, session) is False
    assert _call(conn, "SELECT customer_profile_view(%s)", (session,)) is None
    assert _call(conn, "SELECT auth_session_touch(%s)", (session,)) == {"touched": False}
    assert _call(conn, "SELECT auth_sessions_revoke_all(%s)", (session,)) == {"result": "disabled"}
    throttle = _query(conn, "SELECT scope, attempts FROM auth_throttle ORDER BY scope")
    assert _login(conn, proof)[0] == {"result": "disabled"}
    assert _query(conn, "SELECT scope, attempts FROM auth_throttle ORDER BY scope") == throttle

    _run(conn, "UPDATE auth_settings SET accounts_enabled = TRUE")
    assert _valid(conn, session) is True


def test_link_rechecks_flag_after_waiting_on_customer(accounts, product, peers):
    """Привязка прочитала флаг (включён), встала на FOR SHARE; пока ждала, аккаунты выключили. Проверка (c)
    после ожидания видит выключенный флаг → 'disabled', транзакция заказа откатывается."""
    conn, _ = accounts
    _, session = _register(conn)
    holder, _ = peers()
    buyer, buyer_pid = peers()
    _exec(holder, "SELECT 1 FROM customers WHERE id = %s FOR NO KEY UPDATE", (_customer_id(conn),))

    def order_transaction():
        with buyer.cursor() as cur:
            return _link(cur, session, _new_order(cur, product))

    step = _Background(order_transaction)
    _wait_blocked(conn, buyer_pid)
    _run(conn, "UPDATE auth_settings SET accounts_enabled = FALSE")
    holder.commit()
    step.join()
    assert step.error is None, step.error
    assert step.result == {"result": "disabled"}
    buyer.rollback()
    assert _query(conn, "SELECT count(*) FROM customer_orders") == [(0,)]


def test_password_change_rechecks_flag_after_waiting(accounts, peers):
    conn, _ = accounts
    proof, session = _register(conn)
    holder, _ = peers()
    changer, changer_pid = peers()
    _exec(holder, "SELECT 1 FROM customers WHERE id = %s FOR NO KEY UPDATE", (_customer_id(conn),))
    step = _Background(lambda: _exec(changer, CHANGE, (session, proof, 1, None, None, _proof(), 1, _token())))
    _wait_blocked(conn, changer_pid)
    _run(conn, "UPDATE auth_settings SET accounts_enabled = FALSE")
    holder.commit()
    step.join()
    assert step.error is None, step.error
    assert step.result == {"result": "disabled"}
    changer.commit()
    _run(conn, "UPDATE auth_settings SET accounts_enabled = TRUE")
    assert _login(conn, proof)[0] == {"result": "ok"}                 # пароль не изменён


# 2. Привязывается только новый заказ этой транзакции.
#
# Доказательство опирается на PR2/PR3: заказ видим другим транзакциям только после COMMIT, а при COMMIT
# отложенная проверка orders_check_composition ставит items_sealed = TRUE (снять нельзя — orders_protect_history).
# Значит, видимый незапечатанный заказ создан в текущей транзакции. Ниже — каждая альтернатива отдельно.

def test_link_refuses_uncommitted_order_of_another_transaction(accounts, product, peers):
    conn, _ = accounts
    _, session = _register(conn)
    other, _ = peers()
    with other.cursor() as cur:
        foreign = _new_order(cur, product)                            # чужая транзакция, без COMMIT
    _fails(conn, "SELECT customer_order_link_new(%s, %s)", "только новый заказ", (session, foreign))
    other.rollback()


def test_link_refuses_order_sealed_early_in_this_transaction(accounts, product):
    """SET CONSTRAINTS ... IMMEDIATE запечатывает заказ до COMMIT — привязка уже невозможна."""
    conn, _ = accounts
    _, session = _register(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="только новый заказ"):
        with conn.cursor() as cur:
            order_id = _new_order(cur, product)
            cur.execute("SET CONSTRAINTS orders_check_composition IMMEDIATE")
            cur.execute("SELECT items_sealed FROM orders WHERE id = %s", (order_id,))
            assert cur.fetchone()[0] is True
            _link(cur, session, order_id)
    conn.rollback()


def test_link_refuses_order_rolled_back_to_savepoint(accounts, product):
    conn, _ = accounts
    _, session = _register(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="только новый заказ"):
        with conn.cursor() as cur:
            cur.execute("SAVEPOINT before_order")
            order_id = _new_order(cur, product)
            cur.execute("ROLLBACK TO SAVEPOINT before_order")
            _link(cur, session, order_id)
    conn.rollback()


def test_link_refuses_legacy_status_order(accounts, product):
    conn, _ = accounts
    _, session = _register(conn)
    with pytest.raises(psycopg.errors.RaiseException, match="только новый заказ"):
        with conn.cursor() as cur:
            _link(cur, session, _new_order(cur, product, status="legacy", access=False))
    conn.rollback()


def test_link_twice_in_one_transaction_is_refused(accounts, product):
    conn, _ = accounts
    _, session = _register(conn)
    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.cursor() as cur:
            order_id = _new_order(cur, product)
            assert _link(cur, session, order_id) == {"result": "linked"}
            _link(cur, session, order_id)
    conn.rollback()
    assert _query(conn, "SELECT count(*) FROM customer_orders") == [(0,)]


def test_order_already_linked_by_another_customer_cannot_be_relinked(accounts, product):
    conn, _ = accounts
    _, owner = _register(conn)
    _, other = _register(conn, email="other@example.test")
    with conn.cursor() as cur:
        order_id = _new_order(cur, product)
        _link(cur, owner, order_id)
    conn.commit()
    _fails(conn, "SELECT customer_order_link_new(%s, %s)", "только новый заказ", (other, order_id))
    assert _query(conn, "SELECT customer_id FROM customer_orders WHERE order_id = %s", (order_id,)) \
        == [(_customer_id(conn),)]


def test_link_rollback_leaves_nothing_and_commit_keeps_guest_access(accounts, product):
    conn, _ = accounts
    _, session = _register(conn)
    with conn.cursor() as cur:
        rolled_back = _new_order(cur, product)
        _link(cur, session, rolled_back)
    conn.rollback()
    assert _query(conn, "SELECT count(*) FROM orders WHERE id = %s", (rolled_back,)) == [(0,)]
    assert _query(conn, "SELECT count(*) FROM customer_orders") == [(0,)]

    token = hashlib.sha256(os.urandom(32)).digest()
    with conn.cursor() as cur:
        order_id = _new_order(cur, product, token_sha256=token)
        _link(cur, session, order_id)
    conn.commit()
    assert _query(conn, "SELECT count(*) FROM customer_orders WHERE order_id = %s", (order_id,)) == [(1,)]
    # Гостевой access_token PR3 работает и для привязанного заказа.
    view = _call(conn, "SELECT order_status_view(%s, %s)", (order_id, token))
    assert view["order"]["id"] == order_id


# 3. Одна стоимость bcrypt у всех хэшей: нет различия по времени между существующим и неизвестным email.

def test_cost_change_refused_once_customers_exist(accounts):
    conn, _ = accounts
    _register(conn)
    _fails(conn, "UPDATE auth_settings SET bcrypt_cost = 5", "до первого покупателя")
    assert _query(conn, "SELECT bcrypt_cost, left(dummy_hash, 7) FROM auth_settings") == [(4, "$2a$04$")]


def test_customer_hash_must_use_current_cost(accounts):
    conn, _ = accounts
    _fails(conn, "INSERT INTO customers (email_norm, password_hash, pepper_version) "
                 "VALUES ('c@example.test', ext_pgcrypto.crypt('x', ext_pgcrypto.gen_salt('bf', 5)), 1)",
           "стоимость bcrypt")


def test_all_password_hashes_share_the_dummy_hash_cost(accounts):
    conn, _ = accounts
    old_proof, session = _register(conn, ver=1)
    new_proof = _proof()
    _login(conn, new_proof, ver=2, prev=(old_proof, 1))               # пересчёт под новую версию перца
    _call(conn, CHANGE, (session, new_proof, 2, None, None, _proof(), 2, _token()))
    _register(conn, email="other@example.test")
    prefixes = {p for (p,) in _query(conn, "SELECT left(password_hash, 7) FROM customers")}
    assert prefixes == {_query(conn, "SELECT left(dummy_hash, 7) FROM auth_settings")[0][0]} == {"$2a$04$"}


def test_registration_waits_for_cost_change_and_reports_busy(accounts, peers):
    """Смена стоимости (покупателей ещё нет) держит SHARE на customers до COMMIT: регистрация не вставит
    хэш старой стоимости — ждёт и по lock_timeout отвечает 'busy'; после COMMIT — хэш новой стоимости."""
    conn, _ = accounts
    changer, _ = peers()
    _exec(changer, "UPDATE auth_settings SET bcrypt_cost = 5")
    assert _call(conn, REGISTER, (EMAIL, _proof(), 1, _token())) == {"result": "busy"}
    changer.commit()
    _register(conn)
    assert _query(conn, "SELECT left(password_hash, 7) FROM customers") == [("$2a$05$",)]


def test_cost_change_waits_for_unfinished_first_registration_and_refuses(accounts, peers):
    """Без LOCK TABLE смена стоимости не увидела бы незафиксированную первую регистрацию и прошла бы —
    остался бы хэш старой стоимости. С ней — ждёт COMMIT регистрации и отказывает."""
    conn, _ = accounts
    registrar, _ = peers()
    changer, changer_pid = peers()
    assert _exec(registrar, REGISTER, (EMAIL, _proof(), 1, _token())) == {"result": "created"}
    step = _Background(lambda: _exec(changer, "UPDATE auth_settings SET bcrypt_cost = 6"))
    _wait_blocked(conn, changer_pid)
    registrar.commit()
    step.join()
    assert isinstance(step.error, psycopg.errors.RaiseException) and "до первого покупателя" in str(step.error)
    changer.rollback()
    assert _query(conn, "SELECT bcrypt_cost FROM auth_settings") == [(4,)]


# 4. Время: сроки сессии проверяются на момент проверки (clock_timestamp), а не на начало транзакции.

def test_session_expiring_inside_a_long_transaction_is_invalid(accounts):
    conn, _ = accounts
    _register(conn)
    short = _short_session(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT now()")                                    # транзакция началась до истечения
        time.sleep(1.3)
        cur.execute("SELECT auth_session_resolve(%s)", (short,))
        assert cur.fetchone()[0] == {"valid": False}
    conn.rollback()


def test_link_refused_when_session_expires_while_waiting(accounts, product, peers):
    conn, _ = accounts
    _register(conn)
    short = _short_session(conn)
    holder, _ = peers()
    buyer, buyer_pid = peers()
    _exec(holder, "SELECT 1 FROM customers WHERE id = %s FOR NO KEY UPDATE", (_customer_id(conn),))

    def order_transaction():
        with buyer.cursor() as cur:
            return _link(cur, short, _new_order(cur, product))

    step = _Background(order_transaction)
    _wait_blocked(conn, buyer_pid)
    time.sleep(1.3)                                                    # сессия истекла во время ожидания
    holder.commit()
    step.join()
    assert step.error is None, step.error
    assert step.result == {"result": "session_invalid"}
    buyer.rollback()


def test_password_change_refused_when_session_expires_while_waiting(accounts, peers):
    conn, _ = accounts
    proof, _ = _register(conn)
    short = _short_session(conn)
    holder, _ = peers()
    changer, changer_pid = peers()
    _exec(holder, "SELECT 1 FROM customers WHERE id = %s FOR NO KEY UPDATE", (_customer_id(conn),))
    step = _Background(lambda: _exec(changer, CHANGE, (short, proof, 1, None, None, _proof(), 1, _token())))
    _wait_blocked(conn, changer_pid)
    time.sleep(1.3)                                                    # меньше lock_timeout функции (2 с)
    holder.commit()
    step.join()
    assert step.error is None, step.error
    assert step.result == {"result": "session_invalid"}
    changer.commit()
    assert _login(conn, proof)[0] == {"result": "ok"}                 # пароль не изменён


def test_touch_never_shortens_idle_expiry(accounts):
    conn, _ = accounts
    _, session = _register(conn)
    before = _query(conn, "SELECT idle_expires_at FROM customer_sessions")[0][0]
    _run(conn, "UPDATE auth_settings SET session_idle = INTERVAL '1 day', session_touch_interval = INTERVAL '0'")
    assert _call(conn, "SELECT auth_session_touch(%s)", (session,)) == {"touched": True}
    assert _query(conn, "SELECT idle_expires_at FROM customer_sessions")[0][0] == before


def test_ownership_step_lists_every_shop_object_after_0005(make_schema):
    """deploy/db_roles.py --part ownership передаёт мигратору явный список объектов: в нём все таблицы,
    представления и функции схемы после 0005 (иначе SECURITY DEFINER-функция осталась бы у прежнего владельца)."""
    from deploy import db_roles
    conn, schema = make_schema()
    migrate.apply_migrations(conn, log=_quiet)
    relations = {name for (name,) in _query(conn, "SELECT relname FROM pg_class WHERE relnamespace = %s::regnamespace "
                                                  "AND relkind IN ('r', 'p', 'v')", (schema,))}
    functions = {name for (name,) in _query(conn, "SELECT proname FROM pg_proc WHERE pronamespace = %s::regnamespace",
                                            (schema,))}
    assert relations == set(db_roles.ALL_TABLES)
    assert functions == set(db_roles.SHOP_FUNCTIONS)
