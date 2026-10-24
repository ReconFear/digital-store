"""Генерация SQL deploy/db_roles.py для PR6: pgcrypto в ext_pgcrypto, параметры роли app (Stage 1a),
матрица прав и списки владения для миграции 0005 (Stage 1c).

Только офлайн: тесты вызывают функции, печатающие SQL, main() с перехватом вывода и читают текст файлов
миграций. К PostgreSQL не подключаются, фикстуры базы не используют.
"""

import re
from pathlib import Path

import pytest

from deploy import db_roles

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
SQL_TYPES = {"TEXT": "text", "SMALLINT": "smallint", "INTEGER": "integer", "BIGINT": "bigint", "BYTEA": "bytea",
             "BOOLEAN": "boolean", "UUID": "uuid"}

BAD_NAMES = ["x; DROP ROLE y", "Shop", "a-b", "", 'a"b', "1abc", "a b", "a" * 42,
             # завершающий перевод строки: re.match с "$" его пропустил бы
             "shop\n", "example_test\n"]


def _cli(capsys, *argv):
    assert db_roles.main(list(argv)) == 0
    return capsys.readouterr().out


def _statements(sql):
    return [line for line in sql.splitlines() if line.strip() and not line.lstrip().startswith("--")]


# ---------- --part all не изменился ----------

@pytest.mark.parametrize("database", [None, "digital_store_roles_test"])
def test_part_all_is_unchanged(capsys, database):
    argv = ["--part", "all", "--prefix", "ci_shop"] + (["--database", database] if database else [])
    expected = [db_roles.create_roles_sql("ci_shop")]
    if database:
        expected.append(db_roles.database_sql(database, "ci_shop"))
    expected.append(db_roles.grants_sql("public", "ci_shop"))

    out = _cli(capsys, *argv)

    assert out == "\n".join(expected)
    for marker in ("ext_pgcrypto", "pgcrypto", "idle_in_transaction", "log_parameter"):
        assert marker not in out
    for statement in _statements(out):
        assert not statement.startswith(("CREATE EXTENSION", "CREATE SCHEMA", "ALTER ROLE")), statement


def test_default_part_is_all(capsys):
    assert _cli(capsys) == _cli(capsys, "--part", "all")


def test_grants_do_not_touch_pgcrypto_schema():
    assert "ext_pgcrypto" not in db_roles.grants_sql("public", "digital_store")


# ---------- --part extensions ----------

def test_extensions_sql_exact_statements():
    assert _statements(db_roles.extensions_sql("digital_store")) == [
        "CREATE SCHEMA ext_pgcrypto;",
        "CREATE EXTENSION pgcrypto WITH SCHEMA ext_pgcrypto;",
        "REVOKE ALL ON SCHEMA ext_pgcrypto FROM PUBLIC;",
        "GRANT USAGE ON SCHEMA ext_pgcrypto TO digital_store_migrator;",
    ]


@pytest.mark.parametrize("prefix", ["digital_store", "ci_shop", "shop1"])
def test_extensions_grant_only_to_migrator(prefix):
    sql = db_roles.extensions_sql(prefix)
    grants = [s for s in _statements(sql) if s.startswith("GRANT")]

    assert grants == [f"GRANT USAGE ON SCHEMA ext_pgcrypto TO {prefix}_migrator;"]
    for kind in ("app", "worker", "admin"):
        assert re.search(rf"\b{prefix}_{kind}\b", sql) is None
    for forbidden in ("IF NOT EXISTS", "PASSWORD", "SUPERUSER", "CREATE ROLE", "ALTER ROLE", "DROP", "CASCADE"):
        assert all(forbidden not in s for s in _statements(sql)), forbidden


def test_extensions_cli_matches_function(capsys):
    assert _cli(capsys, "--part", "extensions", "--prefix", "ci_shop") == db_roles.extensions_sql("ci_shop")


# ---------- --part app-settings ----------

DB = "example_pr6_disposable_test"   # условное имя для проверки текста SQL; к базе тесты не обращаются


def test_app_settings_only_alters_app_role():
    sql = db_roles.app_settings_sql(DB, "ci_shop")
    statements = _statements(sql)

    assert statements == [
        f"ALTER ROLE ci_shop_app IN DATABASE {DB} SET idle_in_transaction_session_timeout = '30s';",
        f"ALTER ROLE ci_shop_app IN DATABASE {DB} SET log_parameter_max_length = 0;",
        f"ALTER ROLE ci_shop_app IN DATABASE {DB} SET log_parameter_max_length_on_error = 0;",
    ]
    for kind in ("migrator", "worker", "admin"):
        assert f"ci_shop_{kind}" not in sql
    for forbidden in ("CREATE", "GRANT", "DROP", "PASSWORD", "SUPERUSER"):
        assert all(forbidden not in s for s in statements)


def test_app_settings_statement_timeout_is_comment_only():
    sql = db_roles.app_settings_sql(DB)
    assert "statement_timeout" in sql
    assert all("statement_timeout" not in s for s in _statements(sql))


def test_app_settings_in_database(capsys):
    sql = db_roles.app_settings_sql(DB)
    assert all(s.startswith(f"ALTER ROLE digital_store_app IN DATABASE {DB} SET ") for s in _statements(sql))
    assert _cli(capsys, "--part", "app-settings", "--database", DB) == sql


def test_app_settings_never_cluster_wide():
    # Каждый ALTER ROLE — и исполняемый, и закомментированный пример statement_timeout — ограничен базой.
    sql = db_roles.app_settings_sql(DB)
    alters = [line for line in sql.splitlines() if "ALTER ROLE digital_store_app" in line]
    assert len(alters) == 4
    assert all(f"ALTER ROLE digital_store_app IN DATABASE {DB} SET " in line for line in alters)


def test_app_settings_function_requires_database():
    with pytest.raises(TypeError):
        db_roles.app_settings_sql()  # database — обязательный параметр
    for missing in (None, ""):
        with pytest.raises(ValueError):
            db_roles.app_settings_sql(missing)


def test_app_settings_cli_without_database_fails_without_sql(capsys):
    with pytest.raises(SystemExit) as error:
        db_roles.main(["--part", "app-settings"])
    assert error.value.code != 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "--database" in captured.err


# ---------- проверка идентификаторов ----------

@pytest.mark.parametrize("bad", BAD_NAMES)
def test_bad_prefix_is_rejected(bad):
    with pytest.raises(ValueError):
        db_roles.extensions_sql(bad)
    with pytest.raises(ValueError):
        db_roles.app_settings_sql(DB, bad)


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_bad_database_is_rejected(bad):
    with pytest.raises(ValueError):
        db_roles.app_settings_sql(bad, "digital_store")


@pytest.mark.parametrize("argv", [
    ["--part", "extensions", "--prefix", "x; DROP ROLE y"],
    ["--part", "app-settings", "--database", DB, "--prefix", "Shop"],
    ["--part", "app-settings", "--database", "db; DROP DATABASE digital_store"],
    ["--part", "extensions", "--prefix", "shop\n"],
    ["--part", "app-settings", "--database", DB, "--prefix", "shop\n"],
    ["--part", "app-settings", "--database", "example_test\n"],
    ["--part", "all", "--prefix", "shop\n"],
    ["--part", "all", "--database", "example_test\n"],
    ["--part", "grants", "--schema", "public\n"],
])
def test_cli_rejects_bad_names_without_output(capsys, argv):
    with pytest.raises(ValueError):
        db_roles.main(argv)
    assert capsys.readouterr().out == ""


# ---------- PR6 Stage 1c: матрица прав и владение объектами 0005 (сверка с текстом миграций) ----------

PR6_MATRIX = {
    "app": {"auth_register", "auth_login", "auth_password_change", "auth_session_resolve", "auth_session_touch",
            "auth_session_revoke", "auth_sessions_revoke_all", "customer_profile_view", "customer_orders_view",
            "customer_order_view", "customer_order_link_new"},
    "worker": {"auth_maintenance_purge"},
    "admin": {"admin_customer_lookup", "admin_customer_disable", "admin_customer_revoke_sessions",
              "admin_customer_release_email", "admin_auth_settings_update"},
}
ROLE_FUNCTIONS = {"app": db_roles.APP_FUNCTIONS, "worker": db_roles.WORKER_FUNCTIONS, "admin": db_roles.ADMIN_FUNCTIONS}


def _sql_0005():
    return (MIGRATIONS / "0005_customer_auth.sql").read_text(encoding="utf-8-sig")


def _headers(sql):
    """[(имя, параметры, заголовок до $$)] для каждого CREATE FUNCTION."""
    return re.findall(r"^CREATE (?:OR REPLACE )?FUNCTION ([a-z_]+)\((.*?)\)(.*?)\$\$", sql, flags=re.MULTILINE | re.DOTALL)


def _signature(name, params):
    types = [SQL_TYPES[p.split()[-1].upper()] for p in params.split(",") if p.strip()]
    return f"{name}({', '.join(types)})"


def _names(functions):
    return {f.split("(", 1)[0] for f in functions}


def test_pr6_grant_signatures_match_migration_0005():
    declared = {name: _signature(name, params) for name, params, _ in _headers(_sql_0005())}
    granted = [f for funcs in ROLE_FUNCTIONS.values() for f in funcs if f.split("(", 1)[0] in declared]
    assert len(granted) == sum(len(v) for v in PR6_MATRIX.values())
    for signature in granted:
        assert signature == declared[signature.split("(", 1)[0]]


def test_pr6_execute_matrix_is_exact_and_covers_every_security_definer():
    headers = _headers(_sql_0005())
    declared = {name for name, _, _ in headers}
    definer = {name for name, _, tail in headers if "SECURITY DEFINER" in tail}
    per_role = {role: _names(funcs) & declared for role, funcs in ROLE_FUNCTIONS.items()}
    assert per_role == PR6_MATRIX
    # Каждая SECURITY DEFINER-функция 0005 выдана ровно одной роли; ядра, проверки и триггеры — никому.
    assert set().union(*per_role.values()) == definer
    assert sum(len(v) for v in per_role.values()) == len(definer)


def test_grants_sql_gives_pr6_functions_to_the_right_roles_only():
    sql = db_roles.grants_sql("public", "ci_shop")
    for role, funcs in ROLE_FUNCTIONS.items():
        for signature in funcs:
            if signature.split("(", 1)[0] in set().union(*PR6_MATRIX.values()):
                assert sql.count(f"FUNCTION public.{signature} ") == 1, signature
                assert f"GRANT EXECUTE ON FUNCTION public.{signature} TO ci_shop_{role};" in sql


def test_grants_sql_never_mentions_account_tables_or_pgcrypto():
    sql = db_roles.grants_sql("public", "ci_shop")
    assert set(db_roles.ACCOUNT_TABLES) <= set(db_roles.ALL_TABLES)
    for table in db_roles.ACCOUNT_TABLES:
        assert re.search(rf"\b{table}\b", sql) is None, table
    assert "ext_pgcrypto" not in sql and "pgcrypto" not in sql


def test_ownership_lists_cover_every_migration_object():
    """--part ownership передаёт мигратору явный список: в нём все таблицы, представления и функции 0000–0005
    (иначе SECURITY DEFINER-функция осталась бы у прежнего владельца). Тот же факт на базе проверяет
    test_migration_0005.py::test_ownership_step_lists_every_shop_object_after_0005."""
    text = "\n".join(p.read_text(encoding="utf-8-sig") for p in sorted(MIGRATIONS.glob("*.sql")))
    relations = set(re.findall(r"^\s*CREATE (?:TABLE|VIEW)(?: IF NOT EXISTS)? ([a-z_]+)", text, flags=re.MULTILINE))
    functions = set(re.findall(r"^CREATE (?:OR REPLACE )?FUNCTION ([a-z_]+)\(", text, flags=re.MULTILINE))
    assert relations | {"schema_migrations"} == set(db_roles.ALL_TABLES)
    assert functions == set(db_roles.SHOP_FUNCTIONS)
    assert len(db_roles.SHOP_FUNCTIONS) == len(set(db_roles.SHOP_FUNCTIONS))
