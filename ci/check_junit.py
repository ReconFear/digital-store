"""Строгая проверка отчёта pytest (JUnit XML): ни одного пропуска, xfail, ошибки или падения.

    python ci/check_junit.py <junit.xml> [--min-tests N]

Защищает от «зелёного» прогона, в котором проверки прав тихо пропущены (skip) или помечены xfail.
"""

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import Counter


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("junit")
    parser.add_argument("--min-tests", type=int, default=1)
    args = parser.parse_args(argv)

    outcomes, by_module, bad = Counter(), Counter(), []
    for case in ET.parse(args.junit).iter("testcase"):
        name = f"{case.get('classname')}::{case.get('name')}"
        if case.find("failure") is not None:
            outcome = "failed"
        elif case.find("error") is not None:
            outcome = "error"
        elif case.find("skipped") is not None:
            skipped = case.find("skipped")
            # pytest пишет xfail тоже как <skipped type="pytest.xfail">
            outcome = "xfail" if "xfail" in (skipped.get("type") or "") else "skipped"
        else:
            outcome = "passed"
        outcomes[outcome] += 1
        by_module[(case.get("classname") or "").rsplit(".", 1)[-1], outcome] += 1
        if outcome != "passed":
            bad.append(f"{outcome}: {name}")

    total = sum(outcomes.values())
    print(f"Всего {total}: " + ", ".join(f"{k} {v}" for k, v in sorted(outcomes.items())))
    privileges = {m: by_module[(m, "passed")] for m in ("test_db_privileges", "test_digital_keys_privileges")}
    print(f"Тесты прав PostgreSQL выполнены: {privileges}")
    if bad:
        print("Недопустимые исходы (CI требует только passed):", file=sys.stderr)
        for line in bad[:200]:
            print(f"  {line}", file=sys.stderr)
        return 1
    if total < args.min_tests:
        print(f"Тестов {total} — меньше ожидаемого минимума {args.min_tests}", file=sys.stderr)
        return 1
    if not all(privileges.values()):
        print("Тесты прав PostgreSQL не выполнялись", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
