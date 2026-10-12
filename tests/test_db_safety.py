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


def test_monkeypatch_to_test_database_still_works(monkeypatch):
    # Старые тесты делают именно так — это должно оставаться разрешённым.
    monkeypatch.setenv("DB_NAME", "digital_store_test")

    with database.get_connection() as conn:
        assert conn.info.dbname == "digital_store_test"
