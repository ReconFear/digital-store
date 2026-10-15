"""Тестовые секреты и загрузка ключей для тестов PR5.

Секреты здесь — ТОЛЬКО для тестовой базы: выводятся из фиксированных строк, публичны и не
подходят для настоящего магазина. Настоящие секреты в тесты не попадают (conftest.py очищает
DIGITAL_KEYS_* до загрузки .env).
"""

import base64
import hashlib
import uuid

import database
from commerce.digital_keys import importer
from commerce.digital_keys.crypto import Keyring


def _secret(label):
    return base64.b64encode(hashlib.sha256(f"pytest-only/{label}".encode()).digest()).decode()


TEST_ENV = {
    "DIGITAL_KEYS_ENCRYPTION_KEYS": f"t1:{_secret('enc-t1')},t2:{_secret('enc-t2')}",
    "DIGITAL_KEYS_ACTIVE_KEY": "t1",
    "DIGITAL_KEYS_FINGERPRINT_KEY": f"tf1:{_secret('fp-tf1')}",
}


def keyring(active="t1", env=None):
    values = dict(TEST_ENV if env is None else env)
    values["DIGITAL_KEYS_ACTIVE_KEY"] = active
    return Keyring.from_env(values, need_active=True, need_fingerprint=True)


def delivery_keyring():
    """Как у веб-процесса: только ключи шифрования."""
    return Keyring.from_env({"DIGITAL_KEYS_ENCRYPTION_KEYS": TEST_ENV["DIGITAL_KEYS_ENCRYPTION_KEYS"]})


def register_test_keys(cursor):
    """Вносит тестовые версии в реестр (идемпотентно). Под владельцем схемы — через ядро."""
    for name, value in keyring().check_values().items():
        purpose, key_id = name.split(":", 1)
        cursor.execute("SELECT encryption_key_register_core(%s, %s, %s, 'pytest')", (purpose, key_id, value))


def unique_codes(n, prefix="TEST"):
    return [f"{prefix}-{uuid.uuid4().hex[:10].upper()}-{i}" for i in range(n)]


def stock_keys(product_id, n=None, codes=None, active="t1", skip_duplicates=False, conn=None):
    """Загружает (и коммитит) ключи товару через ядро импорта. -> (codes, report)."""
    codes = list(codes) if codes is not None else unique_codes(n)
    own = conn is None
    conn = conn or database.get_connection()
    try:
        with conn.cursor() as cursor:
            register_test_keys(cursor)
            batch = importer.prepare_batch(keyring(active), product_id, list(enumerate(codes, start=1)))
            report = importer.submit(cursor, batch, "pytest", skip_duplicates, core_actor="pytest")
        if own:
            conn.commit()
    finally:
        if own:
            conn.close()
    return codes, report


# ---------- сценарии заказа (под владельцем схемы, как остальные тесты) ----------

def q(sql, params=None, conn=None):
    """Строки запроса (отдельное соединение с COMMIT, если conn не передано)."""
    own = conn is None
    conn = conn or database.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall() if cursor.description else None
        if own:
            conn.commit()
        return rows
    finally:
        if own:
            conn.close()


def one(sql, params=None, conn=None):
    rows = q(sql, params, conn)
    return rows[0][0] if rows else None


def jsonb(value):
    import json
    return json.loads(value) if isinstance(value, str) else value


def make_digital(make_product, keys=0, **kw):
    """Товар digital_key / digital_keys с keys загруженными ключами (остаток = keys). -> (pid, codes)."""
    pid = make_product(quantity=0, product_type="digital_key", fulfillment_type="digital_keys", **kw)
    codes = stock_keys(pid, keys)[0] if keys else []
    return pid, codes


def new_order(items):
    """Заказ v2 с секретом. -> (order_id, token)."""
    from payments.access import create_order_with_access
    order, token = create_order_with_access(items)
    return order["id"], token


def start_payment(order_id):
    """Платёжная попытка в статусе pending (как после создания платежа у провайдера). -> (id, provider_id)."""
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT total_minor, currency FROM orders WHERE id = %s", (order_id,))
            total, currency = cursor.fetchone()
            cursor.execute("INSERT INTO payments (order_id, provider, idempotence_key, amount_minor, currency) "
                           "VALUES (%s, 'yookassa', gen_random_uuid(), %s, %s) RETURNING id", (order_id, total, currency))
            payment_id = cursor.fetchone()[0]
            provider_id = f"pt-{payment_id}-{uuid.uuid4().hex[:8]}"
            cursor.execute("UPDATE payments SET provider_payment_id = %s, status = 'pending' WHERE id = %s",
                           (provider_id, payment_id))
    return payment_id, provider_id


def record_success(payment_id, provider_id, conn=None):
    """Успешная оплата у провайдера учитывается настоящей функцией PR3. -> ответ функции."""
    own = conn is None
    conn = conn or database.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT amount_minor, currency FROM payments WHERE id = %s", (payment_id,))
            total, currency = cursor.fetchone()
            cursor.execute("SELECT payment_record_provider_state(%s, %s, 'succeeded', %s, %s, TRUE, TRUE, NULL)",
                           (payment_id, provider_id, total, currency))
            result = jsonb(cursor.fetchone()[0])
        if own:
            conn.commit()
        return result
    finally:
        if own:
            conn.close()


def pay(order_id):
    """Учитывает успешную оплату заказа (как платёжный обработчик после проверки у провайдера).
    -> (payment_id, ответ payment_record_provider_state)."""
    payment_id, provider_id = start_payment(order_id)
    return payment_id, record_success(payment_id, provider_id)


def run_worker(**kw):
    """Один проход worker только с модулем digital_keys."""
    from commerce import fulfillment
    from commerce.digital_keys.handler import DigitalKeysHandler
    registered = "digital_keys" in fulfillment.registered_types()
    if not registered:
        fulfillment.register_handler(DigitalKeysHandler())
    try:
        kw.setdefault("retry_delay_seconds", 0)
        return fulfillment.process_pending_fulfillments(**kw)
    finally:
        if not registered:
            fulfillment.unregister_handler("digital_keys")


def tasks(order_id):
    """[(fulfillment_id, status, status_note), ...] задач заказа."""
    return q("SELECT f.id, f.status, f.status_note FROM fulfillments f JOIN order_items i ON i.id = f.order_item_id "
             "WHERE i.order_id = %s ORDER BY f.id", (order_id,))


def stock(product_id):
    """(Q, A, assigned, withdrawn, W)."""
    return tuple(q("SELECT quantity, available, assigned, withdrawn, awaiting FROM digital_key_stock "
                   "WHERE product_id = %s", (product_id,))[0])


def invariant_holds(product_id):
    qty, available, _, _, awaiting = stock(product_id)
    return available >= awaiting + qty


def delivery(order_id, token):
    from commerce.digital_keys.delivery import get_delivery
    return get_delivery(order_id, token, keyring_loader=delivery_keyring)


# ---------- персональный администратор магазина для тестов (временная роль) ----------

import os as _os
import secrets as _secrets

import pytest as _pytest

# Функции, которые получает группа администраторов (как в deploy/db_roles.py, ADMIN_FUNCTIONS PR5).
_ADMIN_PR5_FUNCTIONS = (
    "admin_import_digital_keys(integer, text, text[], text[], text[], text[], text[], text, boolean)",
    "admin_withdraw_digital_key(uuid, text)",
    "admin_resume_fulfillment(bigint, text, boolean)",
    "admin_cancel_fulfillment(bigint, text)",
    "admin_reconcile_digital_stock(integer)",
    "admin_register_encryption_key(text, text, text)",
    "admin_digital_keys_keyring_sample()",
    "admin_whoami()",
)


class ShopAdmin:
    """Временная персональная LOGIN-роль — член группы администраторов магазина (WITH INHERIT TRUE,
    SET FALSE). Административные действия выполняются ТОЛЬКО через admin_*-функции под ней."""

    def __init__(self, name, password, schema):
        self.name, self.password, self.schema = name, password, schema

    def connect(self, lock_timeout="5s"):
        conn = database.psycopg.connect(host=_os.getenv("DB_HOST"), port=_os.getenv("DB_PORT"),
                                        dbname=_os.getenv("DB_NAME"), user=self.name, password=self.password)
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {self.schema}")
            cur.execute(f"SET lock_timeout = '{lock_timeout}'")
        conn.commit()
        return conn

    def call(self, sql, params=None):
        """Одно действие = одна транзакция; результат — первое значение (jsonb разобран)."""
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                value = cur.fetchone()[0] if cur.description else None
            conn.commit()
            return jsonb(value) if isinstance(value, str) and value[:1] in "{[" else value
        finally:
            conn.close()

    def resume(self, fulfillment_id, reason="pytest", release_refund_hold=False):
        return self.call("SELECT admin_resume_fulfillment(%s, %s, %s)", (fulfillment_id, reason, release_refund_hold))

    def cancel(self, fulfillment_id, reason="pytest"):
        return self.call("SELECT admin_cancel_fulfillment(%s, %s)", (fulfillment_id, reason))


def _roles_allowed():
    if _os.getenv("DS_TEST_CREATE_ROLES") != "1":
        return "административные сценарии выполняются под временной ролью администратора: включите DS_TEST_CREATE_ROLES=1"
    if not one("SELECT rolcreaterole OR rolsuper FROM pg_roles WHERE rolname = current_user"):
        return "у тестовой роли нет CREATEROLE — временную роль администратора создать нельзя"
    return None


def _cleanup_roles(created, group_created, schema, settings_inserted):
    conn = database.get_connection()
    try:
        with conn.cursor() as cur:
            for name in created:
                cur.execute(f"REVOKE ALL ON SCHEMA {schema} FROM {name}")
                cur.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM {name}")
                cur.execute(f"REVOKE CONNECT ON DATABASE {one('SELECT current_database()')} FROM {name}")
            if settings_inserted:
                cur.execute("DELETE FROM shop_security_settings WHERE admin_group = %s", (group_created,))
            if group_created:
                cur.execute(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {schema} FROM {group_created}")
                cur.execute(f"REVOKE ALL ON SCHEMA {schema} FROM {group_created}")
                cur.execute(f"REVOKE CONNECT ON DATABASE {one('SELECT current_database()')} FROM {group_created}")
        conn.commit()
        with conn.cursor() as cur:
            for name in list(created) + ([group_created] if group_created else []):
                cur.execute(f"DROP ROLE IF EXISTS {name}")
        conn.commit()
    finally:
        conn.close()


@_pytest.fixture
def shop_admin_factory():
    """Создаёт временных персональных администраторов (и посторонние роли) и удаляет их после теста.

    * Группа администраторов берётся из shop_security_settings — поэтому работает при любом
      префиксе ролей (настроенном через deploy/db_roles.py).
    * Если группа не настроена (тестовая база без ролей магазина), создаётся временная NOLOGIN-группа
      с EXECUTE на admin_*-функции, и она записывается в настройки — только если текущая роль владеет
      таблицами магазина (иначе писать настройки нельзя; тест пропускается). В конце запись удаляется.
    * Проверки базы (shop_require_admin, fulfillments_guard) не ослабляются: роль — обычный член
      группы с наследованием прав, не владелец и не суперпользователь.
    """
    reason = _roles_allowed()
    if reason:
        _pytest.skip(reason)
    schema = one("SELECT current_schema()")
    tag = f"dst_t{_secrets.token_hex(4)}"
    created, state = [], {"group": None, "settings": False}

    def group():
        configured = one("SELECT admin_group FROM shop_security_settings")
        if configured:
            return configured
        if not one("SELECT pg_get_userbyid(relowner) = current_user FROM pg_class WHERE oid = 'digital_keys'::regclass"):
            _pytest.skip("группа администраторов не настроена, а текущая роль не владелец таблиц — настройте роли "
                         "(deploy/db_roles.py --part grants)")
        name = f"{tag}_admins"   # имя группы не совпадает ни с одной создаваемой LOGIN-ролью
        q(f"CREATE ROLE {name} NOLOGIN")
        state["group"] = name
        q(f"GRANT USAGE ON SCHEMA {schema} TO {name}")
        q(f"GRANT CONNECT ON DATABASE {one('SELECT current_database()')} TO {name}")
        for fn in _ADMIN_PR5_FUNCTIONS:
            q(f"GRANT EXECUTE ON FUNCTION {schema}.{fn} TO {name}")
        q("INSERT INTO shop_security_settings (singleton, admin_group) VALUES (TRUE, %s)", (name,))
        state["settings"] = True
        return name

    def login(suffix, member=True, extra_execute=()):
        group()   # группа настроена до создания любой роли: отказ посторонним — именно проверкой членства
        name = f"{tag}_{suffix}"
        password = _secrets.token_urlsafe(16)
        q(f"CREATE ROLE {name} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT PASSWORD '{password}'")
        created.append(name)
        if member:
            admin_group = group()   # ошибки настройки группы — не маскируются пропуском
            try:
                q(f"GRANT {admin_group} TO {name} WITH INHERIT TRUE, SET FALSE")
            except Exception as exc:  # noqa: BLE001 — нет ADMIN OPTION на группу
                _pytest.skip(f"текущая роль не может выдать членство в группе администраторов: {exc}")
        else:
            # Посторонняя роль: может войти и видит схему, членства в группе нет.
            q(f"GRANT CONNECT ON DATABASE {one('SELECT current_database()')} TO {name}")
            q(f"GRANT USAGE ON SCHEMA {schema} TO {name}")
            for fn in extra_execute:
                q(f"GRANT EXECUTE ON FUNCTION {schema}.{fn} TO {name}")
        return ShopAdmin(name, password, schema)

    try:
        yield login
    finally:
        _cleanup_roles(created, state["group"], schema, state["settings"])


@_pytest.fixture
def shop_admin(shop_admin_factory):
    return shop_admin_factory("admin")
