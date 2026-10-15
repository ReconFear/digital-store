"""Общие настройки тестов Digital Store и предохранитель базы данных.

Предохранитель гарантирует, что тесты не могут подключиться к основной базе
(digital_store) или к любой базе, имя которой не оканчивается на "_test".

Как это работает:
1. При загрузке этого файла (pytest грузит conftest.py ДО импорта тестов)
   DB_NAME принудительно ставится в имя тестовой базы. Поэтому load_dotenv()
   в database.py уже не сможет подставить digital_store из .env: python-dotenv
   по умолчанию не перезаписывает заданные переменные окружения.
2. psycopg.connect подменяется на всё время прогона:
   * до подключения проверяется dbname из аргументов или строки подключения;
   * после подключения проверяется фактическое имя базы (conn.info.dbname).
   Любое подключение не к *_test-базе — это ошибка ещё до первого SQL-запроса.
   Кроме того, разрешена только ОДНА база — TEST_DB_NAME (PR5 rev4): подключение к другой
   *_test-базе тоже отклоняется, чтобы тесты не переключались между тестовыми базами.

Взаимодействие с monkeypatch: тесты не должны выставлять DB_NAME сами — база задаётся
только TEST_DB_NAME. Если тест выставит DB_NAME=digital_store или другую *_test-базу,
подключение будет отклонено.
По окончании теста monkeypatch вернёт DB_NAME к тестовому значению.

ВАЖНО: это программная страховка, а не изоляция. Она не заменяет отдельную роль
PostgreSQL для тестов без права подключения к основной базе (см. README,
раздел «Автоматические тесты»). Под суперпользователем postgres у тестов
технически есть доступ ко всем базам.

Имя тестовой базы можно сменить переменной окружения TEST_DB_NAME
(например, для второй тестовой базы); она тоже обязана оканчиваться на "_test".
"""

import os
from contextlib import contextmanager

import psycopg
import psycopg.conninfo
import pytest

TEST_DB_SUFFIX = "_test"


class ProductionDatabaseAccessError(RuntimeError):
    """Попытка тестов подключиться не к тестовой базе."""


def _require_test_db(dbname, where):
    if not dbname or not str(dbname).endswith(TEST_DB_SUFFIX):
        raise ProductionDatabaseAccessError(
            f"Предохранитель тестов: подключение к базе {dbname!r} запрещено ({where}). "
            f"Тесты работают только с базами, имя которых оканчивается на '{TEST_DB_SUFFIX}'."
        )


# Ограничение частоты запросов (http_security.py) в общих тестах API выключено: они делают
# много запросов с одного адреса. Сам ограничитель проверяется отдельно (test_http_security.py)
# на собственном экземпляре, независимо от этой переменной. Платежи в тестах тоже по
# умолчанию выключены — сервис с имитатором ЮKassa создают тесты платежей сами.
os.environ["HTTP_RATE_LIMITS"] = "off"
os.environ["PAYMENTS_ENABLED"] = "false"
# PR5: настоящие секреты цифровых ключей и учётные данные администратора из .env в тесты не
# попадают. Пустое значение не перезаписывается load_dotenv(); тесты задают тестовые секреты явно.
for _name in ("DIGITAL_KEYS_ENCRYPTION_KEYS", "DIGITAL_KEYS_ACTIVE_KEY", "DIGITAL_KEYS_FINGERPRINT_KEY",
              "DIGITAL_KEYS_SECRETS_FILE", "FULFILLMENT_MODULES", "ADMIN_DB_USER", "ADMIN_DB_PASSWORD",
              "ADMIN_DB_HOST", "ADMIN_DB_PORT", "ADMIN_DB_NAME"):
    os.environ[_name] = ""

TEST_DB_NAME = os.environ.get("TEST_DB_NAME", "digital_store_test")
_require_test_db(TEST_DB_NAME, "TEST_DB_NAME")
os.environ["DB_NAME"] = TEST_DB_NAME

_original_connect = psycopg.connect


def _require_selected_test_db(dbname, where):
    """Только ОДНА тестовая база на прогон — TEST_DB_NAME. Другая *_test-база (например, старая
    digital_store_test при TEST_DB_NAME=digital_store_roles_test) тоже отклоняется: иначе тест,
    выставивший DB_NAME сам, незаметно проверял бы другую базу с другой схемой и данными."""
    _require_test_db(dbname, where)
    if dbname != TEST_DB_NAME:
        raise ProductionDatabaseAccessError(
            f"Предохранитель тестов: подключение к базе {dbname!r} запрещено ({where}). "
            f"В этом прогоне разрешена только тестовая база TEST_DB_NAME={TEST_DB_NAME!r}."
        )


def _guarded_connect(conninfo="", *args, **kwargs):
    requested = psycopg.conninfo.conninfo_to_dict(conninfo, **kwargs).get("dbname")
    _require_selected_test_db(requested, "до подключения")

    conn = _original_connect(conninfo, *args, **kwargs)
    try:
        _require_selected_test_db(conn.info.dbname, "после подключения")
    except ProductionDatabaseAccessError:
        conn.close()
        raise
    return conn


psycopg.connect = _guarded_connect


@pytest.fixture
def shared_test_connection(monkeypatch):
    """Одно соединение с тестовой базой на весь тест; в конце всё откатывается.

    database.get_connection подменяется так, что любые функции из database.py
    работают внутри этой же транзакции и видят данные, вставленные тестом.
    Ничего не коммитится: после теста база остаётся в исходном состоянии.
    """
    import database

    conn = database.get_connection()

    @contextmanager
    def same_connection():
        yield conn

    monkeypatch.setattr(database, "get_connection", same_connection)

    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@pytest.fixture
def make_product():
    """Создаёт ЗАКОММИЧЕННЫЕ товары в тестовой базе и полностью удаляет их после теста
    (вместе с заказами, позициями и задачами исполнения, где они встречаются).

    Нужна для проверок, где участвуют несколько соединений (конкурентность, атомарность):
    данные, не зафиксированные в базе, другие соединения не видят.
    """
    import uuid

    import database

    created = []

    def factory(*, quantity=10, price_minor=10000, currency="RUB", is_for_sale=True,
                product_type="other", inventory_mode="tracked", fulfillment_type="manual", name=None):
        name = name or f"pytest-{uuid.uuid4().hex[:8]}"
        with database.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO products (name, price_minor, currency, quantity, is_for_sale,
                                          product_type, inventory_mode, fulfillment_type)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (name, price_minor, currency, quantity, is_for_sale,
                     product_type, inventory_mode, fulfillment_type),
                )
                product_id = cursor.fetchone()[0]
        created.append(product_id)
        return product_id

    yield factory

    if not created:
        return
    with database.get_connection() as conn:
        with conn.cursor() as cursor:
            # История заказов защищена от удаления (миграция 0002); уборка тестовых данных —
            # осознанное исключение только для этой транзакции.
            cursor.execute("SET LOCAL digital_store.allow_history_delete = 'on'")
            cursor.execute(
                "SELECT DISTINCT order_id FROM order_items WHERE product_id = ANY(%s)", (created,)
            )
            order_ids = [row[0] for row in cursor.fetchall()]
            if order_ids:
                # Платёжные данные PR 3 (тоже защищены; удаляются в той же транзакции уборки).
                for table in ("refunds", "money_operations", "audit_log", "payments", "order_access"):
                    cursor.execute(f"DELETE FROM {table} WHERE order_id = ANY(%s)", (order_ids,))
            # PR5: ключи (ссылаются на задачи, позиции и партии) и партии — до задач и товаров.
            cursor.execute("DELETE FROM digital_keys WHERE product_id = ANY(%s)", (created,))
            cursor.execute("DELETE FROM digital_key_batches WHERE product_id = ANY(%s)", (created,))
            cursor.execute("DELETE FROM audit_log WHERE order_id IS NULL AND (details->>'product_id')::int = ANY(%s)",
                           (created,))
            if order_ids:
                cursor.execute(
                    """
                    DELETE FROM fulfillments
                    WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id = ANY(%s))
                    """,
                    (order_ids,),
                )
                cursor.execute("DELETE FROM order_items WHERE order_id = ANY(%s)", (order_ids,))
                cursor.execute("DELETE FROM orders WHERE id = ANY(%s)", (order_ids,))
            cursor.execute("DELETE FROM products WHERE id = ANY(%s)", (created,))


@pytest.fixture
def v1_product(shared_test_connection):
    """v1_product(price=999, quantity=10, is_for_sale=True) -> id товара формата API v1 (цена в целых
    рублях, учёт остатка), созданного в транзакции shared_test_connection (в конце откатывается).
    Старые тесты v1 опираются на него, а не на товары № 1/№ 2 из seed.sql."""
    import uuid

    def factory(price=999, quantity=10, is_for_sale=True):
        with shared_test_connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO products (name, price, quantity, is_for_sale) VALUES (%s, %s, %s, %s) RETURNING id",
                (f"pytest-v1-{uuid.uuid4().hex[:8]}", price, quantity, is_for_sale),
            )
            return cursor.fetchone()[0]

    return factory


@pytest.fixture
def db_value():
    """db_value(sql, params) — одно значение через отдельное соединение (видит только закоммиченное)."""
    import database

    def query(sql, params=None):
        with database.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchone()[0]

    return query
