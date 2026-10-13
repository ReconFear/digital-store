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

Взаимодействие с monkeypatch: тесты могут, как и раньше, делать
monkeypatch.setenv("DB_NAME", "digital_store_test") — это совместимо.
Если тест выставит DB_NAME=digital_store, подключение будет отклонено.
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

TEST_DB_NAME = os.environ.get("TEST_DB_NAME", "digital_store_test")
_require_test_db(TEST_DB_NAME, "TEST_DB_NAME")
os.environ["DB_NAME"] = TEST_DB_NAME

_original_connect = psycopg.connect


def _guarded_connect(conninfo="", *args, **kwargs):
    requested = psycopg.conninfo.conninfo_to_dict(conninfo, **kwargs).get("dbname")
    _require_test_db(requested, "до подключения")

    conn = _original_connect(conninfo, *args, **kwargs)
    try:
        _require_test_db(conn.info.dbname, "после подключения")
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
def db_value():
    """db_value(sql, params) — одно значение через отдельное соединение (видит только закоммиченное)."""
    import database

    def query(sql, params=None):
        with database.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql, params)
                return cursor.fetchone()[0]

    return query
