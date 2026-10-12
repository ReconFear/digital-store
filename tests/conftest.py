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
