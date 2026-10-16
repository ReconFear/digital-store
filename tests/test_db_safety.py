"""Проверки предохранителя тестовой базы (tests/conftest.py).

Тесты на отказ не подключаются к PostgreSQL вообще: подключение
отклоняется до сетевого обращения, поэтому они проходят даже без базы.
"""

import os

import psycopg
import pytest

import database


def test_db_name_is_forced_to_test_database():
    assert os.environ["DB_NAME"].endswith("_test")


@pytest.mark.parametrize(
    "dbname",
    ["digital_store", "digital_store_testing", "postgres", ""],
)
def test_get_connection_refuses_non_test_database(monkeypatch, dbname):
    monkeypatch.setenv("DB_NAME", dbname)

    with pytest.raises(RuntimeError, match="Предохранитель тестов"):
        database.get_connection()


def test_missing_db_name_is_refused(monkeypatch):
    # Без явного имени libpq подставил бы базу по умолчанию — это тоже запрещено.
    monkeypatch.delenv("DB_NAME", raising=False)

    with pytest.raises(RuntimeError, match="Предохранитель тестов"):
        database.get_connection()


def test_direct_psycopg_connect_is_guarded():
    with pytest.raises(RuntimeError, match="Предохранитель тестов"):
        psycopg.connect(dbname="digital_store")

    with pytest.raises(RuntimeError, match="Предохранитель тестов"):
        psycopg.connect("dbname=digital_store")


def test_monkeypatch_to_selected_test_database_still_works(monkeypatch):
    # Явная установка той же базы, что выбрана TEST_DB_NAME, разрешена.
    import conftest
    monkeypatch.setenv("DB_NAME", conftest.TEST_DB_NAME)

    with database.get_connection() as conn:
        assert conn.info.dbname == conftest.TEST_DB_NAME


def test_other_test_database_is_refused(monkeypatch):
    """Другая *_test-база в том же прогоне запрещена (PR5 rev4): тесты не переключаются между базами.
    Отказ — до сетевого обращения, поэтому существование базы не важно."""
    import conftest
    other = "digital_store_roles_test" if conftest.TEST_DB_NAME != "digital_store_roles_test" else "digital_store_test"
    monkeypatch.setenv("DB_NAME", other)
    with pytest.raises(RuntimeError, match="разрешена только тестовая база TEST_DB_NAME"):
        database.get_connection()
    with pytest.raises(RuntimeError, match="разрешена только тестовая база TEST_DB_NAME"):
        psycopg.connect(dbname=other)
