"""Проверка тестовой базы CI ПОСЛЕ pytest (выполняется всегда, даже если тесты упали).

    python ci/check_test_db.py --database digital_store_roles_test --prefix ci_shop --baseline <файл.json>

Ошибка (код 1), если:
  * модель безопасности нарушена (check_invariants: владельцы, SECURITY DEFINER, PUBLIC, admin_group, миграции);
  * роли, членства, ACL базы/схем/таблиц/столбцов/функций, права по умолчанию, триггеры, admin_group или
    журнал миграций отличаются от снимка до тестов — например, остались временные роли dst_t*/dst_*,
    появились новые права, отключён защитный триггер;
  * остались временные схемы тестов.
Только чтение.
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_invariants import InvariantError, check_invariants, connect, diff_snapshots, require_disposable_target, snapshot  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database", required=True)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--baseline", required=True)
    args = parser.parse_args(argv)
    require_disposable_target(args.database, os.environ.get("DB_HOST", ""))
    baseline_path = Path(args.baseline)
    if not baseline_path.is_file():
        raise InvariantError("нет снимка до тестов — подготовка базы не завершилась")
    before = json.loads(baseline_path.read_text(encoding="utf-8"))

    conn = connect(args.database, os.environ["DB_USER"], os.environ.get("DB_PASSWORD"))
    try:
        problems = check_invariants(conn, args.prefix)
        after = snapshot(conn)
        conn.rollback()
    finally:
        conn.close()

    differences = diff_snapshots(before, after)
    leftover_roles = sorted({r[0] for r in after["roles"]} - {r[0] for r in before["roles"]})
    report = {
        "временные роли после тестов": leftover_roles,
        "admin_group": after["admin_group"],
        "миграции": [v for v, _ in after["migrations"]],
        "схемы": [s[0] for s in after["schemas"]],
    }
    print(json.dumps(report, ensure_ascii=False))
    if problems or differences:
        for line in problems:
            print(f"НАРУШЕНИЕ: {line}", file=sys.stderr)
        for line in differences:
            print(f"ИЗМЕНЕНИЕ ПОСЛЕ ТЕСТОВ: {line}", file=sys.stderr)
        return 1
    print("Инварианты безопасности после тестов соблюдены; состояние прав совпадает со снимком до тестов.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except InvariantError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        sys.exit(1)
