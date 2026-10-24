"""Строгая проверка отчёта pytest (JUnit XML) в CI: только passed, обязательные модули выполнены, тестов достаточно.

    python ci/check_junit.py <junit.xml> --min-tests N
    python ci/check_junit.py <junit-g9.xml> --min-tests 7 --profile g9

Код выхода: 0 — всё в порядке; 1 — нарушение политики; 2 — отчёт не прочитан (нет файла, пустой или
испорченный XML, не формат pytest).

Защищает от «зелёного» прогона, в котором проверки прав или тесты с настоящей базой тихо пропущены (skip),
помечены xfail или не выполнялись вовсе.

Формат pytest 9 (junit_family xunit2; проверено на настоящих отчётах — tests/test_ci_check_junit.py):
  * <testsuites><testsuite tests= failures= errors= skipped=> … <testcase classname= name=>;
  * исход — дочерний элемент <testcase>: <failure>; <error> (в том числе сбой teardown и ошибка сбора модуля);
    <skipped> (pytest.skip, пропуск модуля целиком, xfail — type="pytest.xfail");
  * пропуск модуля и ошибка сбора — <testcase classname="" name="tests.test_x">: модуль берётся из name.
    При ошибке сбора pytest не выполняет ни одного теста;
  * тест, упавший и в call, и в teardown, — два <testcase> с одинаковыми classname и name; атрибут tests
    считает элементы <testcase>, а не тесты.
Поэтому тесты считаются по уникальному идентификатору (classname, name): тест выполнен, только если ни у одного
его <testcase> нет <failure>, <error> или <skipped>. Атрибуты <testsuite> сверяются с содержимым: расхождение
(обрезанный или собранный вручную отчёт) — тоже нарушение. XPASS без строгого режима в отчёте неотличим от
passed, поэтому CI запускает pytest с -o xfail_strict=true (XPASS — failure).

Обязательные модули (REQUIRED_MODULES) — проверки прав ролей PostgreSQL (PR3–PR6) и аккаунтов на настоящей базе
(PR6). Минимум выполненных тестов модуля — фактическое число его тестов при сборе на 2026-10-18 (pytest
--collect-only): тихая потеря части тестов модуля — тоже нарушение. Уменьшать минимум — только явной правкой.
Минимумы test_account_maintenance_db и test_customer_auth_invariants_db (Stage 2f-5b, 2f-7) подсчитаны по
исходному коду модулей (функции test_* с учётом @pytest.mark.parametrize), без pytest --collect-only; сверить с
первым полным прогоном CI.

Профиль g9 (PR6 G9) — отдельный обязательный прогон tests/test_account_disconnect_db.py на одноразовой базе CI
(ci/g9_db.py): в отчёте ровно семь ожидаемых тестов (G9_TESTS: classname и name), каждый passed; другой,
недостающий или лишний тест — нарушение. Отсутствие отчёта — код 2. Остальные правила те же.
"""

import argparse
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict

REQUIRED_MODULES = {
    "test_db_privileges": 55,                # права ролей PR3/PR4 (временные роли, DS_TEST_CREATE_ROLES=1)
    "test_digital_keys_privileges": 45,      # права ролей PR5
    "test_customer_auth_privileges": 122,    # права ролей на объекты миграции 0005 (аккаунты PR6)
    "test_account_service_db": 20,           # AccountService на настоящей базе
    "test_account_http_db": 8,               # маршруты /api/account/* на настоящей базе
    "test_account_order_db": 27,             # привязка заказа к аккаунту на настоящей базе
    "test_account_maintenance_db": 13,       # очистка auth_maintenance_purge (accounts.maintenance) на настоящей базе
    "test_customer_auth_invariants_db": 6,   # инварианты 0005 на настоящей базе: эпоха, окно счётчика, блокировки
    "test_account_disconnect": 4,            # Г9 без базы: D1, D3, D4 ×2 (настоящий HTTP-стек, свои дочерние процессы)
}
# Профиль g9: точный состав отдельного прогона (pytest tests/test_account_disconnect_db.py из корня проекта).
_G9_CLASS = "tests.test_account_disconnect_db"
G9_TESTS = frozenset((_G9_CLASS, name) for name in (
    "test_c3_client_disconnect_before_commit_does_not_prevent_commit[c3a_correct_password-fin]",
    "test_c3_client_disconnect_before_commit_does_not_prevent_commit[c3a_correct_password-rst]",
    "test_c3_client_disconnect_before_commit_does_not_prevent_commit[c3b_wrong_password-fin]",
    "test_c3_client_disconnect_before_commit_does_not_prevent_commit[c3b_wrong_password-rst]",
    "test_c4_child_killed_before_commit_rolls_back_and_releases_locks",
    "test_d4_graceful_shutdown_with_open_transaction_commits_and_exits[programmatic_should_exit]",
    "test_d4_graceful_shutdown_with_open_transaction_commits_and_exits[os_signal]",
))
PROFILES = {"main": (REQUIRED_MODULES, None), "g9": ({"test_account_disconnect_db": len(G9_TESTS)}, G9_TESTS)}
SUITE_COUNTS = ("tests", "failures", "errors", "skipped")
_OUTCOME_TAGS = {"failure": "failed", "error": "error", "skipped": "skipped"}
_SEVERITY = ("failed", "error", "xfail", "skipped", "passed")    # исход теста — самый тяжёлый из его записей
MAX_LISTED = 200


class ReportError(Exception):
    """Отчёт не прочитан или не в формате pytest."""


def _suite_count(suite, name):
    value = suite.get(name)
    if value is None or not value.isascii() or not value.isdigit():
        raise ReportError(f"у <testsuite> нет числового атрибута {name}")
    return int(value)


def _record_outcome(case):
    outcomes = []
    for child in case:
        if child.tag in _OUTCOME_TAGS:
            outcome = _OUTCOME_TAGS[child.tag]
            if outcome == "skipped" and "xfail" in (child.get("type") or ""):
                outcome = "xfail"
            outcomes.append(outcome)
    return min(outcomes, key=_SEVERITY.index) if outcomes else "passed"


def read_report(path):
    """-> (записи [(classname, name, исход)], Counter атрибутов <testsuite>, Counter содержимого)."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ReportError(f"отчёт не прочитан: {exc}") from None
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = root.findall("testsuite")
    else:
        raise ReportError(f"корневой элемент <{root.tag}> — не отчёт pytest")
    if not suites:
        raise ReportError("в отчёте нет <testsuite>")
    records, declared, found = [], Counter(), Counter()
    for suite in suites:
        for name in SUITE_COUNTS:
            declared[name] += _suite_count(suite, name)
        for case in suite.findall("testcase"):
            found["tests"] += 1
            for child in case:
                if child.tag in ("failure", "error", "skipped"):
                    found[{"failure": "failures", "error": "errors", "skipped": "skipped"}[child.tag]] += 1
            records.append((case.get("classname") or "", case.get("name") or "", _record_outcome(case)))
    return records, declared, found


def module_of(classname, name):
    """Модуль теста: первая часть пути вида test_* (tests.test_x, tests.test_x.TestClass; у пропуска модуля и
    ошибки сбора classname пуст — путь в name)."""
    parts = (classname or name).split(".")
    for part in parts:
        if part.startswith("test_"):
            return part
    return parts[-1]


def evaluate(path, *, min_tests, required=None, exact=None):
    """-> (код выхода, строки отчёта, нарушения). Код 2 — отчёт не прочитан. exact — точный набор
    (classname, name), который должен быть в отчёте: ни одного недостающего и ни одного лишнего."""
    required = REQUIRED_MODULES if required is None else required
    try:
        records, declared, found = read_report(path)
    except ReportError as exc:
        return 2, [], [str(exc)]

    problems = []
    for name in SUITE_COUNTS:
        if declared[name] != found[name]:
            problems.append(f"атрибут <testsuite> {name}={declared[name]}, а в отчёте {found[name]} — отчёт неполон "
                            "или изменён")
    tests = defaultdict(list)
    for classname, name, outcome in records:
        if not name:
            problems.append("<testcase> без name")
            continue
        tests[(classname, name)].append(outcome)

    outcomes, passed_by_module, bad = Counter(), Counter(), []
    for (classname, name), recorded in tests.items():
        outcome = min(recorded, key=_SEVERITY.index)
        outcomes[outcome] += 1
        if outcome == "passed":
            passed_by_module[module_of(classname, name)] += 1
        else:
            bad.append(f"{outcome}: {classname + '::' if classname else ''}{name}")

    passed = outcomes["passed"]
    lines = [f"Тестов {len(tests)} (записей <testcase> {len(records)}): "
             + (", ".join(f"{k} {v}" for k, v in sorted(outcomes.items())) or "нет"),
             "Обязательные модули (выполнено / минимум): "
             + ", ".join(f"{m} {passed_by_module[m]}/{minimum}" for m, minimum in required.items())]
    if bad:
        problems.append(f"недопустимые исходы ({len(bad)}; CI требует только passed):")
        problems.extend(f"  {line}" for line in sorted(bad)[:MAX_LISTED])
        if len(bad) > MAX_LISTED:
            problems.append(f"  … и ещё {len(bad) - MAX_LISTED}")
    if not tests:
        problems.append("в отчёте нет ни одного теста")
    if passed < min_tests:
        problems.append(f"выполнено тестов {passed} — меньше минимума {min_tests}")
    for module, minimum in required.items():
        if passed_by_module[module] < minimum:
            state = "отсутствует в отчёте" if passed_by_module[module] == 0 and not any(
                module_of(c, n) == module for c, n in tests) else f"выполнено {passed_by_module[module]}"
            problems.append(f"обязательный модуль {module}: {state}, требуется не меньше {minimum}")
    if exact is not None:
        missing, extra = sorted(set(exact) - set(tests)), sorted(set(tests) - set(exact))
        problems.extend(f"нет ожидаемого теста: {classname}::{name}" for classname, name in missing)
        problems.extend(f"лишний тест: {classname + '::' if classname else ''}{name}" for classname, name in extra)
    return (1 if problems else 0), lines, problems


def _positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("ожидается целое число ≥ 1")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("junit")
    parser.add_argument("--min-tests", type=_positive, required=True,
                        help="минимум выполненных (passed) тестов по уникальным идентификаторам")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="main",
                        help="main — полный прогон (REQUIRED_MODULES); g9 — ровно G9_TESTS")
    args = parser.parse_args(argv)
    required, exact = PROFILES[args.profile]
    code, lines, problems = evaluate(args.junit, min_tests=args.min_tests, required=required, exact=exact)
    for line in lines:
        print(line)
    if problems:
        print("Проверка отчёта не пройдена:" if code == 1 else "Отчёт не прочитан:", file=sys.stderr)
        for line in problems:
            print(line, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
