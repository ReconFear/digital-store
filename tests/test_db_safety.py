"""Проверки предохранителя тестовой базы (tests/conftest.py).

Тесты на отказ не подключаются к PostgreSQL вообще: подключение
отклоняется до сетевого обращения, поэтому они проходят даже без базы.
С базой работает только test_monkeypatch_to_selected_test_database_still_works,
и только при явно заданном TEST_DB_NAME.
"""

import os
import socket

import psycopg
import pytest

import database


@pytest.fixture
def no_network(monkeypatch):
    """Отказ обязан случиться до сети: настоящее подключение и любой сокет — провал теста."""
    import conftest

    def forbidden(*args, **kwargs):
        pytest.fail("предохранитель пропустил подключение дальше проверки имени базы")

    assert psycopg.connect is conftest._guarded_connect          # действует предохранитель именно этого conftest
    monkeypatch.setattr(conftest, "_original_connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    return conftest


def test_db_name_is_forced_to_test_database():
    import conftest
    if conftest.TEST_DB_NAME is None:
        assert os.environ["DB_NAME"] == conftest.NO_TEST_DB and not os.environ["DB_NAME"].endswith("_test")
    else:
        assert os.environ["DB_NAME"] == conftest.TEST_DB_NAME and conftest.TEST_DB_NAME.endswith("_test")


@pytest.mark.parametrize(
    "dbname",
    ["digital_store", "digital_store_testing", "postgres", ""],
)
def test_get_connection_refuses_non_test_database(monkeypatch, no_network, dbname):
    monkeypatch.setenv("DB_NAME", dbname)

    with pytest.raises(RuntimeError, match="Предохранитель тестов"):
        database.get_connection()


def test_missing_db_name_is_refused(monkeypatch, no_network):
    # Без явного имени libpq подставил бы базу по умолчанию — это тоже запрещено.
    monkeypatch.delenv("DB_NAME", raising=False)

    with pytest.raises(RuntimeError, match="Предохранитель тестов"):
        database.get_connection()


def test_direct_psycopg_connect_is_guarded(no_network):
    with pytest.raises(RuntimeError, match="Предохранитель тестов"):
        psycopg.connect(dbname="digital_store")

    with pytest.raises(RuntimeError, match="Предохранитель тестов"):
        psycopg.connect("dbname=digital_store")


@pytest.mark.skipif(not os.environ.get("TEST_DB_NAME"), reason="нужна явно заданная TEST_DB_NAME")
def test_monkeypatch_to_selected_test_database_still_works(monkeypatch):
    # Явная установка той же базы, что выбрана TEST_DB_NAME, разрешена.
    import conftest
    monkeypatch.setenv("DB_NAME", conftest.TEST_DB_NAME)

    with database.get_connection() as conn:
        assert conn.info.dbname == conftest.TEST_DB_NAME


def test_other_test_database_is_refused(monkeypatch, no_network):
    """Другая *_test-база в том же прогоне запрещена (PR5 rev4): тесты не переключаются между базами.
    Отказ — до сетевого обращения, поэтому существование базы не важно. Без TEST_DB_NAME проверяется та же
    ветка с подставленным именем выбранной базы (подключения нет: отказ раньше)."""
    conftest = no_network
    if conftest.TEST_DB_NAME is None:
        monkeypatch.setattr(conftest, "TEST_DB_NAME", "digital_store_pr6_offline_test")
    other = "digital_store_roles_test" if conftest.TEST_DB_NAME != "digital_store_roles_test" else "digital_store_test"
    monkeypatch.setenv("DB_NAME", other)
    with pytest.raises(RuntimeError, match="разрешена только тестовая база TEST_DB_NAME="):
        database.get_connection()
    with pytest.raises(RuntimeError, match="разрешена только тестовая база TEST_DB_NAME="):
        psycopg.connect(dbname=other)


@pytest.mark.parametrize("dbname", ["digital_store_test", "digital_store_pr6_stage1_test"])
def test_without_test_db_name_every_connection_is_refused_before_network(monkeypatch, no_network, dbname):
    """Fail-closed: без TEST_DB_NAME отклоняется даже *_test-база — до настоящего подключения и до сокета."""
    conftest = no_network
    monkeypatch.setattr(conftest, "TEST_DB_NAME", None)
    monkeypatch.setenv("DB_NAME", dbname)
    with pytest.raises(RuntimeError, match="TEST_DB_NAME, а она не задана"):
        database.get_connection()
    with pytest.raises(RuntimeError, match="TEST_DB_NAME, а она не задана"):
        psycopg.connect(dbname=dbname)
    with pytest.raises(RuntimeError, match="TEST_DB_NAME, а она не задана"):
        psycopg.connect(f"dbname={dbname} host=127.0.0.1")
