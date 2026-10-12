"""Целостность заказа и статусов: попытки обойти защиту прямым SQL (ревью PR 2, rev2–rev3).

Правило: состав заказа записывается только до COMMIT транзакции, создавшей заказ; при COMMIT
позиции есть, их сумма равна итогу, валюта одна, и заказ закрывается (items_sealed = TRUE).
После этого позиции и заказ нельзя изменить; удалить можно только заказ целиком и только
осознанно; TRUNCATE запрещён. Статусы меняются только по разрешённым переходам.
Создание заказов v1 и v2 продолжает работать.

SQL-уровень проверяется во временных схемах (make_schema), уровень приложения —
в тестовой базе на закоммиченных данных (make_product).
"""

import pytest

import database
import migrate
import test_migrations
from commerce.orders import create_order
from test_migrations import _query, _quiet, _run

make_schema = test_migrations.make_schema

ITEM_COLUMNS = ("order_id, product_id, product_name, product_type, fulfillment_type, "
                "quantity, unit_price_minor, currency, line_total_minor")


def _item_sql(order_ref, unit=500, qty=1, currency="RUB", product_id=1):
    return (f"INSERT INTO order_items ({ITEM_COLUMNS}) VALUES "
            f"({order_ref}, {product_id}, 'x', 'other', 'manual', {qty}, {unit}, '{currency}', {unit * qty})")


@pytest.fixture
def shop(make_schema):
    conn, _ = make_schema()
    migrate.apply_migrations(conn, log=_quiet)
    _run(conn, "INSERT INTO products (name, price, quantity, is_for_sale) VALUES ('A', 999, 10, TRUE), ('B', 5, 10, TRUE)")
    _run(conn, "INSERT INTO orders (product_id, price) VALUES (1, 999)")  # заказ v1, id = 1
    return conn


def _fails(conn, sql, match):
    with pytest.raises(Exception, match=match):
        _run(conn, sql)
    conn.rollback()


def _composition(conn, order_id=1):
    return _query(conn, "SELECT product_id, quantity, line_total_minor FROM order_items WHERE order_id = %s ORDER BY id",
                  (order_id,))


# ---------- обходы после создания заказа ----------

def test_cannot_add_item_to_existing_order(shop):
    _fails(shop, _item_sql(1, unit=0, product_id=2), "уже зафиксирован")  # даже бесплатную позицию
    assert _composition(shop) == [(1, 1, 99900)]


def test_cannot_delete_item(shop):
    _fails(shop, "DELETE FROM order_items WHERE order_id = 1", "не удаляются")
    assert _composition(shop) == [(1, 1, 99900)]


def test_cannot_update_item(shop):
    _fails(shop, "UPDATE order_items SET quantity = 2, line_total_minor = 199800 WHERE order_id = 1", "неизменяемы")


def test_cannot_delete_order(shop):
    _fails(shop, "DELETE FROM orders WHERE id = 1", "не удаляются")


def test_cannot_truncate_history_even_with_cascade(shop):
    _fails(shop, "TRUNCATE order_items, orders, fulfillments", "TRUNCATE")
    _fails(shop, "TRUNCATE orders CASCADE", "TRUNCATE")
    assert _composition(shop) == [(1, 1, 99900)]


def test_cannot_change_money_of_order_or_reopen_it(shop):
    _fails(shop, "UPDATE orders SET total_minor = 0 WHERE id = 1", "неизменяемы")
    _fails(shop, "UPDATE orders SET currency = 'USD' WHERE id = 1", "неизменяемы")
    _fails(shop, "UPDATE orders SET items_sealed = FALSE WHERE id = 1", "открыть его нельзя")


def test_cannot_reopen_order_then_add_item(shop):
    # Попытка «переоткрыть» заказ и дописать позицию в своей транзакции целиком отклоняется.
    _fails(shop, "UPDATE orders SET items_sealed = FALSE WHERE id = 1; " + _item_sql(1, unit=0, product_id=2),
           "открыть его нельзя")
    assert _composition(shop) == [(1, 1, 99900)]
    assert _query(shop, "SELECT items_sealed FROM orders WHERE id = 1") == [(True,)]


def test_new_order_is_sealed_at_commit(shop):
    assert _query(shop, "SELECT items_sealed FROM orders WHERE id = 1") == [(True,)]


def test_status_and_payment_time_can_still_change(shop):
    _run(shop, "UPDATE orders SET status = 'paid', paid_at = now() WHERE id = 1")
    assert _query(shop, "SELECT status FROM orders WHERE id = 1") == [("paid",)]


# ---------- проверки при COMMIT транзакции создания ----------

def test_order_without_items_cannot_be_committed(shop):
    _fails(shop, "INSERT INTO orders (status, currency, total_minor) VALUES ('pending', 'RUB', 100)", "без позиций")


def test_total_must_equal_sum_of_items_at_commit(shop):
    _fails(shop, "INSERT INTO orders (status, currency, total_minor) VALUES ('pending', 'RUB', 600); "
                 + _item_sql("currval('orders_id_seq')", unit=500), "не равен сумме")


def test_item_currency_must_match_order(shop):
    _fails(shop, "INSERT INTO orders (status, currency, total_minor) VALUES ('pending', 'RUB', 500); "
                 + _item_sql("currval('orders_id_seq')", unit=500, currency="USD"), "валюта позиции")


def test_composition_can_be_written_inside_savepoint_of_creating_transaction(shop):
    # pg_current_xact_id() — id верхней транзакции, поэтому SAVEPOINT в транзакции создания не мешает.
    _run(shop, "INSERT INTO orders (status, currency, total_minor) VALUES ('pending', 'RUB', 1000); "
               "SAVEPOINT s; " + _item_sql("currval('orders_id_seq')", unit=500, qty=2) + "; RELEASE SAVEPOINT s")
    assert _query(shop, "SELECT o.total_minor, count(i.id) FROM orders o JOIN order_items i ON i.order_id = o.id "
                        "WHERE o.id = (SELECT max(id) FROM orders) GROUP BY o.total_minor") == [(1000, 1)]


def test_correct_multi_item_order_commits(shop):
    _run(shop, "INSERT INTO orders (status, currency, total_minor) VALUES ('pending', 'RUB', 1005); "
               + _item_sql("currval('orders_id_seq')", unit=500, qty=2, product_id=1) + "; "
               + _item_sql("currval('orders_id_seq')", unit=5, qty=1, product_id=2))
    assert _query(shop, "SELECT count(*) FROM order_items i JOIN orders o ON o.id = i.order_id "
                        "WHERE o.total_minor = 1005") == [(2,)]


# ---------- осознанное удаление (уборка тестовых данных) ----------

def test_deleting_only_items_fails_at_commit_even_when_deletion_is_allowed(shop):
    _fails(shop, "SET LOCAL digital_store.allow_history_delete = 'on'; DELETE FROM order_items WHERE order_id = 1",
           "без позиций")
    assert _composition(shop) == [(1, 1, 99900)]


def test_deleting_one_item_of_multi_item_order_fails_at_commit(shop):
    _run(shop, "INSERT INTO orders (status, currency, total_minor) VALUES ('pending', 'RUB', 1005); "
               + _item_sql("currval('orders_id_seq')", unit=500, qty=2, product_id=1) + "; "
               + _item_sql("currval('orders_id_seq')", unit=5, qty=1, product_id=2))
    order_id = _query(shop, "SELECT max(id) FROM orders")[0][0]

    _fails(shop, "SET LOCAL digital_store.allow_history_delete = 'on'; "
                 f"DELETE FROM order_items WHERE order_id = {order_id} AND product_id = 2", "не равен сумме")
    assert len(_composition(shop, order_id)) == 2


def test_truncate_is_refused_even_when_deletion_is_allowed(shop):
    _fails(shop, "SET LOCAL digital_store.allow_history_delete = 'on'; TRUNCATE order_items, orders, fulfillments",
           "TRUNCATE")
    assert _composition(shop) == [(1, 1, 99900)]


def test_history_delete_needs_explicit_setting_scoped_to_one_transaction(shop):
    # Атомарная уборка заказа целиком: позиции и заказ в одной транзакции — проходит.
    _run(shop, "SET LOCAL digital_store.allow_history_delete = 'on'; "
               "DELETE FROM order_items WHERE order_id = 1; DELETE FROM orders WHERE id = 1")
    assert _query(shop, "SELECT count(*) FROM orders") == [(0,)]
    assert _query(shop, "SELECT count(*) FROM order_items") == [(0,)]

    _run(shop, "INSERT INTO orders (product_id, price) VALUES (1, 999)")
    _fails(shop, "DELETE FROM orders", "не удаляются")  # SET LOCAL не пережил COMMIT


# ---------- перенос истории в миграции ----------

def test_historical_orders_are_sealed_after_migration(make_schema):
    conn, _ = make_schema()
    _run(conn, test_migrations.OLD_STRUCTURE_SQL)  # старая база с заказом
    migrate.apply_migrations(conn, log=_quiet)

    assert _composition(conn) == [(1, 1, 99900)]
    _fails(conn, _item_sql(1, unit=0), "уже зафиксирован")
    _fails(conn, "DELETE FROM order_items", "не удаляются")


# ---------- уровень приложения: v1 и v2 продолжают работать, обход отклоняется ----------

def test_v2_order_is_sealed_after_creation(make_product, db_value):
    a = make_product(price_minor=1000)
    b = make_product(price_minor=0)
    order = create_order([(a, 2)])

    with pytest.raises(Exception, match="уже зафиксирован"):
        with database.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(_item_sql(order["id"], unit=0, product_id=b))
    with pytest.raises(Exception, match="не удаляются"):
        with database.get_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM order_items WHERE order_id = %s", (order["id"],))

    assert db_value("SELECT count(*) FROM order_items WHERE order_id = %s", (order["id"],)) == 1
    assert db_value("SELECT total_minor FROM orders WHERE id = %s", (order["id"],)) == 2000


def test_v1_order_creation_still_works(make_product, db_value):
    pid = make_product(price_minor=50000)

    order = database.create_order_in_db(pid)

    assert order["status"] == "pending" and order["price"] == 500
    assert db_value("SELECT total_minor FROM orders WHERE id = %s", (order["id"],)) == 50000


# ---------- статусы и время оплаты ----------

ALLOWED_TRANSITIONS = [
    ("pending", "paid"), ("pending", "requires_action"), ("pending", "cancelled"),
    ("requires_action", "paid"), ("requires_action", "cancelled"),
]
FORBIDDEN_TRANSITIONS = [
    ("paid", "pending"), ("paid", "requires_action"), ("paid", "cancelled"),
    ("cancelled", "pending"), ("cancelled", "paid"),
    ("requires_action", "pending"),
    ("legacy", "pending"), ("legacy", "paid"),
]


def _order_in_status(conn, status):
    """Новый заказ, доведённый до status разрешёнными переходами."""
    initial = "legacy" if status == "legacy" else "pending"
    _run(conn, f"INSERT INTO orders (product_id, price, status) VALUES (1, 999, '{initial}')")
    order_id = _query(conn, "SELECT max(id) FROM orders")[0][0]
    path = {"pending": [], "legacy": [], "paid": ["paid"], "cancelled": ["cancelled"],
            "requires_action": ["requires_action"]}[status]
    for step in path:
        paid_at = ", paid_at = now()" if step == "paid" else ""
        _run(conn, f"UPDATE orders SET status = '{step}'{paid_at} WHERE id = {order_id}")
    return order_id


@pytest.mark.parametrize("old, new", ALLOWED_TRANSITIONS)
def test_allowed_status_transitions(shop, old, new):
    order_id = _order_in_status(shop, old)
    paid_at = ", paid_at = now()" if new == "paid" else ""

    _run(shop, f"UPDATE orders SET status = '{new}'{paid_at} WHERE id = {order_id}")

    assert _query(shop, f"SELECT status FROM orders WHERE id = {order_id}") == [(new,)]


@pytest.mark.parametrize("old, new", FORBIDDEN_TRANSITIONS)
def test_forbidden_status_transitions(shop, old, new):
    order_id = _order_in_status(shop, old)
    paid_at = ", paid_at = now()" if new == "paid" else ""

    _fails(shop, f"UPDATE orders SET status = '{new}'{paid_at} WHERE id = {order_id}", "запрещён")
    assert _query(shop, f"SELECT status FROM orders WHERE id = {order_id}") == [(old,)]


def test_new_order_cannot_start_as_paid_or_with_paid_at(shop):
    _fails(shop, "INSERT INTO orders (product_id, price, status) VALUES (1, 999, 'paid')", "pending")
    _fails(shop, "INSERT INTO orders (product_id, price, paid_at) VALUES (1, 999, now())", "paid_at")


def test_paid_requires_payment_time_and_payment_time_is_frozen(shop):
    order_id = _order_in_status(shop, "pending")
    _fails(shop, f"UPDATE orders SET status = 'paid' WHERE id = {order_id}", "нужно paid_at")
    _fails(shop, f"UPDATE orders SET paid_at = now() WHERE id = {order_id}", "paid_at")

    _run(shop, f"UPDATE orders SET status = 'paid', paid_at = now() WHERE id = {order_id}")
    _fails(shop, f"UPDATE orders SET paid_at = now() + interval '1 day' WHERE id = {order_id}", "paid_at")
    _fails(shop, f"UPDATE orders SET paid_at = NULL WHERE id = {order_id}", "paid_at")


def test_historical_paid_order_keeps_unknown_payment_time(make_schema):
    conn, _ = make_schema()
    _run(conn, test_migrations.OLD_STRUCTURE_SQL)
    _run(conn, test_migrations.MANUAL_001_SQL)
    _run(conn, "INSERT INTO orders (product_id, price, status) VALUES (1, 999, 'paid')")
    migrate.apply_migrations(conn, log=_quiet)

    assert _query(conn, "SELECT status, paid_at, items_sealed FROM orders WHERE id = 2") == [("paid", None, True)]
    _fails(conn, "UPDATE orders SET paid_at = now() WHERE id = 2", "paid_at")
    _fails(conn, "UPDATE orders SET status = 'pending' WHERE id = 2", "запрещён")


# ---------- логическая копия базы (pg_dump / pg_restore) ----------

def _split_schema_sql():
    text = (test_migrations.PROJECT_DIR / "schema.sql").read_text(encoding="utf-8-sig")
    marker = "-- Функции и триггеры"
    return text[:text.index(marker)], text[text.index(marker):]


def test_closed_orders_stay_closed_after_logical_restore(make_schema):
    """Восстановление как у pg_restore: таблицы -> данные (COPY без триггеров) -> функции и триггеры.
    Закрытость состава хранится в данных (items_sealed), а не в номерах транзакций кластера,
    поэтому в восстановленной базе (с любым счётчиком транзакций) старый заказ остаётся закрытым."""
    source, _ = make_schema()
    migrate.apply_migrations(source, log=_quiet)
    _run(source, "INSERT INTO products (name, price, quantity, is_for_sale) VALUES ('A', 999, 10, TRUE), ('B', 5, 10, TRUE)")
    _run(source, "INSERT INTO orders (product_id, price) VALUES (1, 999)")
    products = _query(source, "SELECT id, name, price, quantity, is_for_sale, price_minor FROM products ORDER BY id")
    orders = _query(source, "SELECT id, product_id, price, status, currency, total_minor, items_sealed FROM orders")
    items = _query(source, f"SELECT id, {ITEM_COLUMNS} FROM order_items")

    restored, _ = make_schema()
    pre_data, post_data = _split_schema_sql()
    _run(restored, pre_data)
    with restored.cursor() as cur:
        for row in products:
            cur.execute("INSERT INTO products (id, name, price, quantity, is_for_sale, price_minor) "
                        "VALUES (%s, %s, %s, %s, %s, %s)", row)
        for row in orders:
            cur.execute("INSERT INTO orders (id, product_id, price, status, currency, total_minor, items_sealed) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)", row)
        for row in items:
            cur.execute(f"INSERT INTO order_items (id, {ITEM_COLUMNS}) OVERRIDING SYSTEM VALUE "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", row)
        cur.execute("SELECT setval(pg_get_serial_sequence('orders', 'id'), 1)")
    restored.commit()
    _run(restored, post_data)

    _fails(restored, _item_sql(1, unit=0, product_id=2), "уже зафиксирован")
    _fails(restored, "UPDATE orders SET items_sealed = FALSE WHERE id = 1", "открыть его нельзя")
    _run(restored, "INSERT INTO orders (product_id, price) VALUES (1, 999)")  # новые заказы работают
    assert _query(restored, "SELECT id, items_sealed FROM orders ORDER BY id") == [(1, True), (2, True)]


def test_triggers_work_with_empty_search_path(shop):
    # Так работает pg_restore; функции сами знают свою схему (SET search_path FROM CURRENT).
    schema = _query(shop, "SELECT current_schema()")[0][0]
    _fails(shop, f"SET LOCAL search_path = ''; INSERT INTO {schema}.order_items ({ITEM_COLUMNS}) "
                 f"VALUES (1, 2, 'x', 'other', 'manual', 1, 0, 'RUB', 0)", "уже зафиксирован")
    _run(shop, f"SET LOCAL search_path = ''; INSERT INTO {schema}.orders (product_id, price) VALUES (1, 999)")
    assert _query(shop, "SELECT count(*) FROM orders WHERE items_sealed") == [(2,)]


def test_integrity_does_not_depend_on_transaction_ids(shop):
    # Номера транзакций (xid) уникальны только внутри одного кластера; после pg_dump/pg_restore
    # в другой кластер они могут совпасть (проверено вручную, см. PR2_NOTES_REV3.md).
    schema = _query(shop, "SELECT current_schema()")[0][0]
    sources = _query(shop, "SELECT string_agg(pg_get_functiondef(p.oid), ' ') FROM pg_proc p "
                           "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s", (schema,))[0][0]
    columns = _query(shop, "SELECT string_agg(data_type, ' ') FROM information_schema.columns WHERE table_schema = %s",
                     (schema,))[0][0]
    for marker in ("pg_current_xact_id", "txid_current", "xmin"):
        assert marker not in sources
    assert "xid" not in columns


# ---------- подмена таблиц через временную схему pg_temp (ревью rev3) ----------
#
# PostgreSQL ищет временную схему сессии ПЕРВОЙ, если pg_temp не указан в пути явно.
# В rev3 функции имели путь «схема магазина» (SET search_path FROM CURRENT) — временные
# таблицы orders / order_items / products подменяли настоящие. Исправление: путь
# pg_catalog, <схема>, pg_temp (блок «5б» в 0002). Каждый сценарий ниже сначала
# воспроизводится с путём rev3, затем отклоняется после применения блока из миграции.

TRIGGER_FUNCTIONS = (
    "products_sync_legacy_price", "orders_fill_legacy_columns", "orders_create_legacy_item",
    "orders_protect_history", "orders_guard_status", "orders_protect_delete",
    "order_items_protect_history", "order_history_protect_truncate",
    "order_items_guard_insert", "orders_check_composition",
)


def _schema(conn):
    return _query(conn, "SELECT current_schema()")[0][0]


def _use_rev3_search_path(conn):
    schema = _schema(conn)
    for fn in TRIGGER_FUNCTIONS:
        _run(conn, f"ALTER FUNCTION {schema}.{fn}() SET search_path = {schema}")


def _apply_hardening_from_migration(conn):
    text = (test_migrations.PROJECT_DIR / "migrations" / "0002_universal_core.sql").read_text(encoding="utf-8-sig")
    block = text[text.index("-- ---------- 5б."):text.index("-- ---------- 6. проверка переноса")]
    _run(conn, block)


def _new_sealed_order(conn, schema):
    _run(conn, f"INSERT INTO {schema}.orders (product_id, price) VALUES (1, 999)")
    return _query(conn, f"SELECT max(id) FROM {schema}.orders")[0][0]


def _attempt(conn, sql):
    """Атака выполняется в ОТДЕЛЬНОМ новом соединении, как у настоящего атакующего.
    В той же сессии PL/pgSQL мог бы использовать план, закэшированный до появления
    временной таблицы, и маскировать уязвимость (так и было при первой версии теста)."""
    attacker = database.get_connection()
    try:
        with attacker.cursor() as cursor:
            cursor.execute(f"SET search_path TO {_schema(conn)}")
            cursor.execute(sql)
        attacker.commit()
        return True
    except Exception:  # noqa: BLE001 — отказ базы = обход не удался
        attacker.rollback()
        return False
    finally:
        attacker.close()


def _scenario_fake_unsealed_order(conn, schema):
    """Временная orders с items_sealed = FALSE -> позиция в настоящий закрытый заказ."""
    order_id = _new_sealed_order(conn, schema)
    _attempt(conn, "CREATE TEMP TABLE orders (id INT, currency TEXT, total_minor BIGINT, items_sealed BOOLEAN) "
                   f"ON COMMIT DROP; INSERT INTO pg_temp.orders VALUES ({order_id}, 'RUB', 99900, FALSE); "
                   f"INSERT INTO {schema}.order_items ({ITEM_COLUMNS}) "
                   f"VALUES ({order_id}, 2, 'подброшено', 'other', 'manual', 1, 0, 'RUB', 0)")
    return _query(conn, f"SELECT count(*) FROM {schema}.order_items WHERE order_id = %s", (order_id,))[0][0] > 1


def _scenario_fake_items_for_commit_check(conn, schema):
    """Временная order_items подменяет данные проверки при COMMIT -> заказ без позиций."""
    order_id = _new_sealed_order(conn, schema)
    _attempt(conn, "SET LOCAL digital_store.allow_history_delete = 'on'; "
                   "CREATE TEMP TABLE order_items (order_id INT, line_total_minor BIGINT, currency TEXT) ON COMMIT DROP; "
                   f"INSERT INTO pg_temp.order_items VALUES ({order_id}, 99900, 'RUB'); "
                   f"DELETE FROM {schema}.order_items WHERE order_id = {order_id}")
    return _query(conn, f"SELECT count(*) FROM {schema}.order_items WHERE order_id = %s", (order_id,))[0][0] == 0


def _scenario_sealing_goes_to_temp_table(conn, schema):
    """Временная orders перехватывает закрытие нового заказа -> настоящий остаётся открытым."""
    next_id = _query(conn, f"SELECT max(id) + 1 FROM {schema}.orders")[0][0]
    _attempt(conn, "CREATE TEMP TABLE orders (id INT, currency TEXT, total_minor BIGINT, items_sealed BOOLEAN) "
                   f"ON COMMIT DROP; INSERT INTO pg_temp.orders VALUES ({next_id}, 'RUB', 99900, FALSE); "
                   f"INSERT INTO {schema}.orders (product_id, price) VALUES (1, 999)")
    rows = _query(conn, f"SELECT items_sealed FROM {schema}.orders WHERE id = %s", (next_id,))
    return rows == [(False,)]


def _scenario_fake_product_snapshot(conn, schema):
    """Временная products подменяет название товара в позиции заказа v1."""
    _attempt(conn, "CREATE TEMP TABLE products (id INT, name TEXT, product_type TEXT, fulfillment_type TEXT) "
                   "ON COMMIT DROP; INSERT INTO pg_temp.products VALUES (1, 'ПОДДЕЛКА', 'other', 'manual'); "
                   f"INSERT INTO {schema}.orders (product_id, price) VALUES (1, 999)")
    rows = _query(conn, f"SELECT product_name FROM {schema}.order_items ORDER BY id DESC LIMIT 1")
    return rows == [("ПОДДЕЛКА",)]


TEMP_SHADOWING_SCENARIOS = [
    _scenario_fake_unsealed_order,
    _scenario_fake_items_for_commit_check,
    _scenario_sealing_goes_to_temp_table,
    _scenario_fake_product_snapshot,
]


@pytest.mark.parametrize("scenario", TEMP_SHADOWING_SCENARIOS, ids=lambda f: f.__name__.removeprefix("_scenario_"))
def test_temp_table_shadowing_reproduced_on_rev3_and_closed_by_migration(shop, scenario):
    schema = _schema(shop)

    _use_rev3_search_path(shop)
    assert scenario(shop, schema) is True, "обход должен воспроизводиться при пути rev3"

    _apply_hardening_from_migration(shop)
    assert scenario(shop, schema) is False, "после исправления обход должен отклоняться"


@pytest.mark.parametrize("scenario", TEMP_SHADOWING_SCENARIOS, ids=lambda f: f.__name__.removeprefix("_scenario_"))
def test_temp_table_shadowing_is_refused_after_migration(shop, scenario):
    assert scenario(shop, _schema(shop)) is False


def test_trigger_functions_have_trusted_search_path_in_test_schema(shop):
    schema = _schema(shop)
    configs = dict(_query(shop, "SELECT p.proname, array_to_string(p.proconfig, ';') FROM pg_proc p "
                                "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s", (schema,)))
    assert set(configs) == set(TRIGGER_FUNCTIONS)
    assert set(configs.values()) == {f"search_path=pg_catalog, {schema}, pg_temp"}


def test_trigger_functions_have_trusted_search_path_in_public(db_value):
    # Тестовая база, установленная migrate.py в схему public.
    configs = db_value(
        "SELECT string_agg(DISTINCT array_to_string(p.proconfig, ';'), '|') FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.proname = ANY(%s)",
        (list(TRIGGER_FUNCTIONS),),
    )
    assert configs == "search_path=pg_catalog, public, pg_temp"
