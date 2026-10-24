"""PR6 Stage 2f-2: ci/check_junit.py — строгая проверка отчёта pytest в CI. Офлайн: без базы и без .env.

Три части:
  * искусственные отчёты JUnit — каждый вид нарушения отдельно и полностью успешный отчёт;
  * настоящие отчёты pytest (отдельный процесс pytest на маленьком наборе во временной папке): формат, на котором
    основан подсчёт (пропуск модуля, ошибка сбора, классы, параметры, сбой teardown, xfail/XPASS);
  * .github/workflows/ci.yml: dotenv отключён, xfail_strict, check_junit.py с порогом не меньше 1600.
"""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ci_check_junit", PROJECT_DIR / "ci" / "check_junit.py")
check_junit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_junit)

REQUIRED = check_junit.REQUIRED_MODULES
CI_MIN_TESTS = 1600
CHILD = {"failure": '<failure message="assert False">trace</failure>',
         "error": '<error message="failed on setup">trace</error>',
         "skip": '<skipped type="pytest.skip" message="skipped">reason</skipped>',
         "xfail": '<skipped type="pytest.xfail" message="x" />',
         "collection_skip": '<skipped message="collection skipped">reason</skipped>',
         "collection_error": '<error message="collection failure">ImportError</error>'}


# ---------- искусственные отчёты в формате pytest 9 ----------

def _case(classname, name, outcome=None):
    body = CHILD[outcome] if outcome else ""
    return f"<testcase classname={quoteattr(classname)} name={quoteattr(name)} time=\"0.01\">{body}</testcase>"


def _full_cases(skip_module=None, minus_one=None):
    """Отчёт полностью успешного прогона CI: обязательные модули в полном объёме и прочие тесты до 1641."""
    cases = []
    for module, minimum in REQUIRED.items():
        if module == skip_module:
            continue
        count = minimum - (1 if module == minus_one else 0)
        cases += [_case(f"tests.{module}", f"test_{index}") for index in range(count)]
    cases += [_case("tests.test_api_v2.TestGroup", "test_inside"), _case("tests.test_api_v2", "test_param[a.b]")]
    cases += [_case("tests.test_other", f"test_{index}") for index in range(1641 - len(cases))]
    return cases


def _report(tmp_path, cases, **override):
    counts = {"tests": len(cases), "failures": sum("<failure" in c for c in cases),
              "errors": sum("<error" in c for c in cases), "skipped": sum("<skipped" in c for c in cases)}
    counts.update(override)
    attributes = " ".join(f'{name}="{value}"' for name, value in counts.items())
    path = tmp_path / "junit.xml"
    path.write_text('<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests">'
                    f'<testsuite name="pytest" {attributes} time="1.0">{"".join(cases)}</testsuite></testsuites>',
                    encoding="utf-8")
    return path


def _run(path, min_tests=CI_MIN_TESTS, capsys=None):
    code = check_junit.main([str(path), "--min-tests", str(min_tests)])
    output = capsys.readouterr() if capsys is not None else None
    return code, (output.out + output.err if output else "")


def test_fully_successful_report_passes(tmp_path, capsys):
    code, output = _run(_report(tmp_path, _full_cases()), capsys=capsys)
    assert code == 0, output
    assert "Тестов 1641 (записей <testcase> 1641): passed 1641" in output
    assert "test_customer_auth_privileges 122/122" in output and "test_account_order_db 27/27" in output


@pytest.mark.parametrize("module", list(REQUIRED))
def test_missing_required_module_fails(tmp_path, capsys, module):
    code, output = _run(_report(tmp_path, _full_cases(skip_module=module)), min_tests=1, capsys=capsys)
    assert code == 1 and f"обязательный модуль {module}: отсутствует в отчёте" in output


def test_required_module_below_its_minimum_fails(tmp_path, capsys):
    code, output = _run(_report(tmp_path, _full_cases(minus_one="test_customer_auth_privileges")), min_tests=1,
                        capsys=capsys)
    assert code == 1 and "test_customer_auth_privileges: выполнено 121, требуется не меньше 122" in output


@pytest.mark.parametrize("outcome", ["skip", "xfail", "failure", "error"])
def test_any_non_passed_outcome_fails(tmp_path, capsys, outcome):
    cases = _full_cases() + [_case("tests.test_api", "test_extra", outcome)]
    code, output = _run(_report(tmp_path, cases), capsys=capsys)
    expected = {"skip": "skipped", "xfail": "xfail", "failure": "failed", "error": "error"}[outcome]
    assert code == 1 and f"{expected}: tests.test_api::test_extra" in output


@pytest.mark.parametrize("outcome", ["collection_skip", "collection_error"])
def test_skipped_or_broken_module_is_not_counted_as_present(tmp_path, capsys, outcome):
    """Модуль, пропущенный целиком (нет PYTHON_DOTENV_DISABLED) или не собранный, — <testcase classname="">."""
    cases = _full_cases(skip_module="test_account_order_db") + [_case("", "tests.test_account_order_db", outcome)]
    code, output = _run(_report(tmp_path, cases), min_tests=1, capsys=capsys)
    assert code == 1 and "tests.test_account_order_db" in output
    assert "обязательный модуль test_account_order_db: выполнено 0, требуется не меньше 27" in output


def test_a_test_with_a_failed_record_is_not_passed_even_if_another_record_passed(tmp_path, capsys):
    """Тест, упавший в teardown, может дать несколько <testcase> с одним идентификатором."""
    cases = _full_cases() + [_case("tests.test_api", "test_twice"), _case("tests.test_api", "test_twice", "error")]
    code, output = _run(_report(tmp_path, cases), capsys=capsys)
    assert code == 1 and "error: tests.test_api::test_twice" in output


def test_too_few_executed_tests_fail(tmp_path, capsys):
    code, output = _run(_report(tmp_path, _full_cases()), min_tests=1642, capsys=capsys)
    assert code == 1 and "выполнено тестов 1641 — меньше минимума 1642" in output


def test_duplicate_records_are_counted_once(tmp_path, capsys):
    cases = _full_cases()
    duplicated = cases + cases[-10:]                                   # 1651 запись, но 1641 тест
    code, output = _run(_report(tmp_path, duplicated), min_tests=1645, capsys=capsys)
    assert code == 1 and "выполнено тестов 1641 — меньше минимума 1645" in output


def test_module_names_match_whole_components_only(tmp_path):
    """tests.test_account_http не засчитывается за test_account_http_db; класс засчитывается своему модулю."""
    cases = [_case("tests.test_account_http", f"test_{i}") for i in range(8)]
    cases += [_case("tests.test_account_http_db.TestGroup", "test_0")]
    code, _, problems = check_junit.evaluate(_report(tmp_path, cases), min_tests=1, required={"test_account_http_db": 2})
    assert code == 1 and problems == ["обязательный модуль test_account_http_db: выполнено 1, "
                                      "требуется не меньше 2"]
    assert check_junit.module_of("tests.test_x.TestClass", "test_y[a.b]") == "test_x"
    assert check_junit.module_of("", "tests.test_x") == "test_x"


def test_report_without_tests_fails(tmp_path, capsys):
    code, output = _run(_report(tmp_path, []), min_tests=1, capsys=capsys)
    assert code == 1 and "в отчёте нет ни одного теста" in output


@pytest.mark.parametrize("override", [{"tests": 1640}, {"failures": 1}, {"errors": 1}, {"skipped": 1}])
def test_suite_attributes_must_match_the_content(tmp_path, capsys, override):
    """Обрезанный или исправленный вручную отчёт: атрибуты <testsuite> расходятся с записями."""
    code, output = _run(_report(tmp_path, _full_cases(), **override), capsys=capsys)
    assert code == 1 and "отчёт неполон или изменён" in output


@pytest.mark.parametrize("content", [
    "",                                                                         # пустой файл
    "<testsuites><testsuite",                                                   # обрезанный XML
    "не XML",
    '<?xml version="1.0"?><html><body/></html>',                               # не отчёт pytest
    "<testsuites/>",                                                            # нет <testsuite>
    '<testsuites><testsuite name="pytest" tests="x" failures="0" errors="0" skipped="0"/></testsuites>',
    '<testsuites><testsuite name="pytest" failures="0" errors="0" skipped="0"/></testsuites>',
], ids=["пустой", "обрезанный", "не XML", "чужой корень", "без testsuite", "нечисловой атрибут", "нет атрибута"])
def test_unreadable_report_is_exit_code_2(tmp_path, capsys, content):
    path = tmp_path / "junit.xml"
    path.write_text(content, encoding="utf-8")
    code, output = _run(path, min_tests=1, capsys=capsys)
    assert code == 2 and "Отчёт не прочитан" in output


def test_missing_report_file_is_exit_code_2(tmp_path, capsys):
    code, output = _run(tmp_path / "нет.xml", min_tests=1, capsys=capsys)
    assert code == 2 and "Отчёт не прочитан" in output


@pytest.mark.parametrize("value", ["0", "-5", "abc"])
def test_min_tests_must_be_a_positive_integer(tmp_path, value):
    with pytest.raises(SystemExit) as exit_info:
        check_junit.main([str(_report(tmp_path, _full_cases())), "--min-tests", value])
    assert exit_info.value.code == 2


def test_min_tests_is_required(tmp_path):
    with pytest.raises(SystemExit) as exit_info:
        check_junit.main([str(_report(tmp_path, _full_cases()))])
    assert exit_info.value.code == 2


# ---------- настоящие отчёты pytest ----------

_PASSING = '''
import pytest

@pytest.mark.parametrize("value", ["a.b", "c[d]"])
def test_param(value):
    assert value

class TestGroup:
    def test_inside(self):
        assert True
'''


def _real_report(tmp_path, files, *pytest_args):
    """Отдельный процесс pytest во временной папке (вне проекта: conftest магазина не загружается)."""
    suite = tmp_path / "suite"
    (suite / "tests").mkdir(parents=True)
    (suite / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    for name, text in files.items():
        (suite / "tests" / name).write_text(text, encoding="utf-8")
    report = tmp_path / "junit.xml"
    subprocess.run([sys.executable, "-I", "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
                    f"--junitxml={report}", *pytest_args], cwd=suite, capture_output=True, timeout=120)
    return report


REAL_REQUIRED = {"test_customer_auth_privileges": 3, "test_account_order_db": 3}


def test_real_pytest_report_of_a_successful_run_passes(tmp_path):
    report = _real_report(tmp_path, {"test_customer_auth_privileges.py": _PASSING, "test_account_order_db.py": _PASSING})
    code, lines, problems = check_junit.evaluate(report, min_tests=6, required=REAL_REQUIRED)
    assert (code, problems) == (0, [])
    assert lines[0] == "Тестов 6 (записей <testcase> 6): passed 6"


def test_real_pytest_report_rejects_module_skip_xfail_xpass_and_teardown_errors(tmp_path):
    skipped_module = 'import pytest\npytest.skip("без PYTHON_DOTENV_DISABLED", allow_module_level=True)\n'
    marks = '''
import pytest

@pytest.fixture
def bad_teardown():
    yield
    raise RuntimeError("teardown")

@pytest.mark.xfail(reason="x")
def test_xfail():
    assert False

@pytest.mark.xfail(reason="x")
def test_xpass():
    assert True

def test_fail_and_teardown(bad_teardown):
    assert False

def test_skip():
    pytest.skip("s")
'''
    report = _real_report(tmp_path, {"test_customer_auth_privileges.py": _PASSING,
                                     "test_account_order_db.py": skipped_module, "test_marks.py": marks},
                          "-o", "xfail_strict=true")
    code, _, problems = check_junit.evaluate(report, min_tests=3, required=REAL_REQUIRED)
    text = "\n".join(problems)
    assert code == 1
    for expected in ("skipped: tests.test_account_order_db", "xfail: tests.test_marks::test_xfail",
                     "failed: tests.test_marks::test_xpass", "failed: tests.test_marks::test_fail_and_teardown",
                     "skipped: tests.test_marks::test_skip",
                     "обязательный модуль test_account_order_db: выполнено 0, требуется не меньше 3"):
        assert expected in text, (expected, text)
    assert "атрибут <testsuite>" not in text                          # настоящий отчёт согласован


def test_real_pytest_report_with_a_collection_error_fails(tmp_path):
    report = _real_report(tmp_path, {"test_customer_auth_privileges.py": _PASSING,
                                     "test_account_order_db.py": _PASSING, "test_broken.py": "import no_such_module_xyz\n"})
    code, lines, problems = check_junit.evaluate(report, min_tests=1, required=REAL_REQUIRED)
    assert code == 1 and "  error: tests.test_broken" in problems
    assert lines[0] == "Тестов 1 (записей <testcase> 1): error 1"           # при ошибке сбора тесты не выполняются


# ---------- .github/workflows/ci.yml ----------

WORKFLOW = PROJECT_DIR / ".github" / "workflows" / "ci.yml"


def _job_env(text):
    block = text[text.index("\n    env:\n"):text.index("\n    steps:\n")]
    return dict(re.findall(r"^      ([A-Z_]+): \"?([^\"\n#]*?)\"?\s*(?:#.*)?$", block, flags=re.MULTILINE))


def test_workflow_disables_dotenv_for_every_step_and_checks_it():
    text = WORKFLOW.read_text(encoding="utf-8")
    env = _job_env(text)
    assert env["PYTHON_DOTENV_DISABLED"] == "1"                       # уровень job: все шаги, импорт всех модулей
    assert text.count("PYTHON_DOTENV_DISABLED") >= 2 and "dotenv.load_dotenv(canary) is False" in text
    assert text.index("Verify python-dotenv is disabled") < text.index("Prepare PostgreSQL test database")


def test_workflow_uses_only_its_disposable_container():
    text = WORKFLOW.read_text(encoding="utf-8")
    env = _job_env(text)
    assert "image: postgres:18" in text and env["DB_HOST"] == "localhost"
    assert env["TEST_DB_NAME"] == env["DB_NAME"] and env["TEST_DB_NAME"].endswith("_test")
    assert env["DS_TEST_CREATE_ROLES"] == "1" and env["DS_TEST_SUPERUSER"] == env["DB_USER"]
    configuration = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "pull_request_target" not in configuration and "secrets." not in configuration


def test_workflow_runs_pytest_strictly_and_checks_the_report():
    text = WORKFLOW.read_text(encoding="utf-8")
    pytest_run = re.search(r"run: python -m pytest (.*)", text).group(1)
    assert "-o xfail_strict=true" in pytest_run and '--junitxml="$RUNNER_TEMP/junit.xml"' in pytest_run
    check = re.search(r'run: python ci/check_junit\.py "\$RUNNER_TEMP/junit\.xml" --min-tests (\d+)\s*$', text,
                      flags=re.MULTILINE)
    assert check and int(check.group(1)) >= CI_MIN_TESTS
    step = text[text.rindex("- name:", 0, check.start()):check.start()]
    assert "if: ${{ !cancelled() }}" in step                          # отчёт проверяется и после падения тестов


def test_required_modules_exist_in_the_test_suite():
    for module in REQUIRED:
        assert (PROJECT_DIR / "tests" / f"{module}.py").is_file(), module
    assert {"test_customer_auth_privileges", "test_account_service_db", "test_account_http_db",
            "test_account_order_db", "test_account_maintenance_db", "test_customer_auth_invariants_db",
            "test_db_privileges", "test_digital_keys_privileges"} == set(REQUIRED)
