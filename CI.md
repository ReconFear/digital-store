# CI (GitHub Actions)

Файл: `.github/workflows/ci.yml`. Запускается на Pull Request в `master`, на push в любые ветки, кроме `master`, и вручную (`workflow_dispatch`).

## Что делает

1. **Checkout** без сохранения токена в `.git/config`. Права workflow — только `contents: read`, секреты репозитория не используются.
2. **`git diff --check`** — изменения PR относительно базовой ветки. CR в конце строки не считается ошибкой: файлы проекта в CRLF.
3. **Нет `.env` и дампов** среди файлов репозитория (`.env.example` разрешён).
4. **Python 3.14**, зависимости из `requirements.txt`, проверка, что используется настоящий psycopg (бинарная реализация).
   **`.env` не читается ни в одном шаге:** `PYTHON_DOTENV_DISABLED=1` задан в окружении всего job (`database.py` и `cors_settings.py` вызывают `load_dotenv()` при импорте; все параметры CI заданы в `ci.yml` явно). Отдельный шаг проверяет это на контрольном файле `.env` во временной папке: python-dotenv не должен его прочитать. Без этой переменной модули аккаунтов с базой (`test_account_http_db.py`, `test_account_order_db.py`, `test_account_maintenance_db.py`, `test_customer_auth_invariants_db.py`) пропускаются целиком, и шаг 7 отклонит отчёт.
5. **PostgreSQL 18** — одноразовый service-контейнер. Скрипт `ci/prepare_test_db.py` готовит `digital_store_roles_test` по модели `DB_ROLES.md` штатными средствами:
   - роли `ci_shop_*` — `deploy/db_roles.py` с нестандартным префиксом. Мигратор без `SUPERUSER`, `app`, `worker`, `NOLOGIN`-группа `admin`;
   - `REVOKE CONNECT … FROM PUBLIC` и `CONNECT` ролям магазина (`database_sql`);
   - PR6: `pgcrypto` в схеме `ext_pgcrypto` (`extensions_sql`) — до миграций; `USAGE` только у мигратора;
   - миграции 0000–0005 — `migrate.py apply` **под мигратором**. Владелец всех таблиц и `SECURITY DEFINER`-функций — мигратор;
   - права и `shop_security_settings.admin_group` (`grants_sql`), затем `seed.sql` под мигратором;
   - проверка модели безопасности и снимок прав (JSON без секретов).
6. **`python -m pytest -q -ra -o xfail_strict=true tests --ignore=tests/test_account_disconnect_db.py`** с `PYTHON_DOTENV_DISABLED=1`, `TEST_DB_NAME=DB_NAME=digital_store_roles_test` (база внутри контейнера), `DS_TEST_CREATE_ROLES=1`, `DS_TEST_SUPERUSER=postgres`. `xfail_strict`: XPASS — ошибка (в JUnit без строгого режима он неотличим от успеха). Единственное исключение — `test_account_disconnect_db.py` (Г9 C3/C4/D4 на базе): его дочерний процесс принимает только лабораторию и отдельную одноразовую базу G9 и выполняется в обязательном отдельном прогоне (шаг 9). Четыре сценария Г9 без базы (`test_account_disconnect.py`) выполняются здесь.
7. **`ci/check_junit.py --min-tests 1600`** — выполняется и после падения тестов. Отклоняет отчёт (код 1), если:
   - у любого теста `skipped` (в том числе пропуск модуля целиком), `xfail`, `failure` или `error` (в том числе ошибка сбора модуля и сбой teardown). Тесты считаются по уникальному идентификатору (`classname`, `name`): у теста может быть несколько записей `<testcase>`, и он засчитывается, только если успешны все;
   - выполнено меньше 1600 тестов (при сборе на 2026-10-18 — 1641; запас — на осознанное удаление тестов);
   - не выполнен обязательный модуль или выполнено меньше его тестов, чем минимум в `REQUIRED_MODULES` (минимум — число тестовых случаев модуля с учётом параметризации):

     | Модуль | Минимум | Что проверяет |
     |---|---|---|
     | `test_db_privileges` | 55 | права ролей PR3/PR4 под временными ролями |
     | `test_digital_keys_privileges` | 45 | права ролей PR5 |
     | `test_customer_auth_privileges` | 122 | права ролей на объекты миграции 0005 (аккаунты PR6) |
     | `test_account_service_db` | 20 | `AccountService` на настоящей базе |
     | `test_account_http_db` | 8 | маршруты `/api/account/*` на настоящей базе |
     | `test_account_order_db` | 27 | привязка заказа к аккаунту на настоящей базе |
     | `test_account_maintenance_db` | 13 | очистка `auth_maintenance_purge` (`accounts.maintenance`) на настоящей базе |
     | `test_customer_auth_invariants_db` | 6 | инварианты миграции 0005 на настоящей базе: эпоха сессии, окно счётчика входа, блокировки входа |
     | `test_account_disconnect` | 4 | Г9 без базы: D1, D3, D4 ×2 — настоящий HTTP-стек в uvicorn, свои дочерние процессы |

     Минимумы первых шести модулей — фактическое число тестов при сборе `pytest --collect-only` 18.10.2026. Минимумы `test_account_maintenance_db` и `test_customer_auth_invariants_db` подсчитаны статически по исходному коду (функции `test_*` с учётом `@pytest.mark.parametrize`), без запуска pytest; в CI эти модули ещё не выполнялись, подсчёт нужно подтвердить первым полным прогоном CI.

   - атрибуты `<testsuite>` (`tests`, `failures`, `errors`, `skipped`) не совпадают с записями — отчёт обрезан или изменён.

   Код 2 — отчёт не прочитан: нет файла, пустой или испорченный XML, не формат pytest. Проверка покрыта офлайн-тестами `tests/test_ci_check_junit.py`, в том числе на настоящих отчётах pytest. Локально (в разрешённой лаборатории `digital_store_pr6_stage1_test` под ролью `pr6_stage1_owner`, с `PYTHON_DOTENV_DISABLED=1` и явным `TEST_DB_NAME`) модули аккаунтов с базой выполняются: они работают во временных схемах и новых ролей не создают. Пропускаются локально только тесты прав PostgreSQL (`DS_TEST_CREATE_ROLES=1`): им нужны временные роли кластера и `CREATEROLE`, которого у роли лаборатории нет. Поэтому тесты прав выполняются только в изолированном CI, и зелёный CI — единственное место, где они обязаны пройти.
8. **`ci/check_test_db.py`** — выполняется **всегда**, даже после падения тестов. Падает, если:
   - нарушена модель безопасности: владельцы объектов, `SECURITY DEFINER` у суперпользователя или с `EXECUTE` у `PUBLIC`, `search_path`, `CONNECT`/`CREATE`/права на таблицы у `PUBLIC`, `admin_group`, журнал миграций с 0005 и контрольными суммами;
   - PR6: `pgcrypto` не в `ext_pgcrypto`, схема не у владельца базы, в ней посторонние функции или `USAGE` у кого-то кроме мигратора; у ролей магазина `CREATE` на базу или схему, `TEMPORARY`, любые права на таблицы аккаунтов; перечень вызываемых функций роли не совпадает с матрицей `deploy/db_roles.py`; у функции не ровно один доверенный `search_path` или параметры, кроме `lock_timeout = 2s` у функций с этапом блокировок; столбец `*_sha256` без проверки длины; аккаунты или регистрация включены; хэш покупателя другой стоимости bcrypt;
   - отличается от снимка до тестов хоть что-то из: роли и их атрибуты (например, остались `dst_t*`), членства, действующие права на базу, схемы, таблицы, столбцы и функции, права по умолчанию, состояние триггеров, `admin_group`, миграции, временные схемы.
9. **G9 — отдельный обязательный прогон `tests/test_account_disconnect_db.py`** (семь тестов: C3 ×4, C4, D4 ×2) на отдельной одноразовой базе. Шаги — после шага 8, поэтому его снимок новую роль и базу не видит:
   - **`ci/g9_db.py prepare`** — только если шаг 8 успешен. Суперпользователем контейнера создаёт роль `ds_g9_ci_owner` (`LOGIN`, без `SUPERUSER`/`CREATEDB`/`CREATEROLE`/`REPLICATION`/`BYPASSRLS`, случайный пароль), базу `digital_store_g9_ci_test` (владелец — `postgres`; `CONNECT` и `TEMPORARY` у `PUBLIC` отозваны; роли — `CONNECT` и `CREATE`), `pgcrypto` в `ext_pgcrypto` этой базы с `USAGE` только у роли и маркер базы `ds-g9-disposable-ci`. До любых изменений требует **всех** признаков одноразового кластера этого job: GitHub-hosted runner, снимок шага 5 в `RUNNER_TEMP`, подключение клиента к `127.0.0.1:5432` при адресе сервера не на loopback (порт проброшен в контейнер), PostgreSQL 18, запущенный меньше 2 часов назад, суперпользователь `DB_USER`, базы кластера — ровно `postgres`, `template0`, `template1`, `digital_store_roles_test`, роли — ровно роли снимка шага 5 (а `prepare_test_db.py` отказывает в непустом кластере). К `digital_store_roles_test` скрипт не подключается;
   - **pytest только этого модуля** — с `DB_HOST=127.0.0.1`, ролью и базой G9, `DS_TEST_CREATE_ROLES=0`; дочерний процесс принимает ровно два профиля (лаборатория и эта база) и на каждом соединении проверяет базу, роль и её атрибуты, владельца и маркер базы, `USAGE` на `ext_pgcrypto`, адрес со стороны клиента (`conn.info`) и порт сервера;
   - **`ci/check_junit.py --profile g9`** — выполняется и после падения: в отчёте ровно семь ожидаемых тестов (`G9_TESTS`), все passed; недостающий, лишний, skipped, xfail, failure или error — код 1; нет отчёта (подготовка не выполнялась или упала) — код 2;
   - **`ci/g9_db.py check`** — выполняется **всегда**, только чтение: нет схем `ds_mig_*` и соединений `ds-g9-*`, атрибуты и членства роли, владелец, права, маркер базы, её схемы и расширения, роли и базы кластера совпадают со снимком prepare; нет снимка — ошибка.

   `continue-on-error` не используется: отсутствие или неполнота прогона G9 делает CI красным.

## Учётные данные

Пароль суперпользователя в `ci.yml` (`ci-disposable-postgres`) — публичное значение одноразового контейнера, который существует только во время job на раннере GitHub. Это не секрет и не пароль какой-либо настоящей базы. Пароль мигратора генерируется случайно внутри `prepare_test_db.py`, никуда не выводится и не сохраняется. Пароль роли G9 `ds_g9_ci_owner` генерируется случайно в `ci/g9_db.py prepare`, маскируется (`::add-mask::`) до любого вывода и передаётся только шагу pytest через `$GITHUB_ENV` (`G9_DB_PASSWORD`); в снимок, репозиторий и JUnit он не попадает. У ролей `app`/`worker` паролей нет: тесты прав создают свои временные роли со случайными паролями и удаляют их.

Тесты подключаются суперпользователем контейнера. Он нужен тестам прав, чтобы создавать временные роли, выдавать им `CONNECT` и членство в группе администраторов. Так же они проверялись на Windows. Предохранитель `tests/conftest.py` разрешает подключение только к `TEST_DB_NAME`.

`DS_TEST_CREATE_ROLES=1` и подключение суперпользователем допустимы **только** в одноразовом контейнере: роли создаются на уровне кластера. В локальной лаборатории (`digital_store_pr6_stage1_test`, роль `pr6_stage1_owner` без `CREATEROLE`) эти тесты пропускаются — так и должно быть; права роли лаборатории не расширяются.

## Локальный запуск тех же проверок

> **Внимание.** Только в **новом пустом одноразовом** кластере PostgreSQL, созданном для этой проверки и удаляемом после неё. Существующие локальные базы — `digital_store`, `digital_store_test`, `digital_store_roles_test`, `digital_store_pr6_proto_test`, `digital_store_pr6_stage1_test` и любые другие — использовать **запрещено**, в том числе одноимённую `digital_store_roles_test` локального сервера: имя ниже относится к базе **внутри** контейнера. `.env` не используется: все параметры задаются явно, `PYTHON_DOTENV_DISABLED=1`.

Порядок:

1. Запустить одноразовый контейнер на отдельном порту, чтобы ни одна команда не попала в локальный сервер PostgreSQL (порт 5432):

   ```bash
   docker run --rm -d --name ds-ci-pg -e POSTGRES_PASSWORD=ci-disposable-postgres -p 127.0.0.1:55432:5432 postgres:18
   ```

2. Выполнить те же шаги, что CI, с явными параметрами этого контейнера:

   ```bash
   export PYTHON_DOTENV_DISABLED=1 DB_HOST=localhost DB_PORT=55432 DB_USER=postgres DB_PASSWORD=ci-disposable-postgres
   export TEST_DB_NAME=digital_store_roles_test DB_NAME=digital_store_roles_test PAYMENTS_ENABLED=false
   python ci/prepare_test_db.py --database "$TEST_DB_NAME" --prefix ci_shop --baseline /tmp/baseline.json
   DS_TEST_CREATE_ROLES=1 DS_TEST_SUPERUSER=postgres DS_TEST_SUPERUSER_PASSWORD="$DB_PASSWORD" \
     python -m pytest -q -ra -o xfail_strict=true tests --ignore=tests/test_account_disconnect_db.py --junitxml=/tmp/junit.xml
   python ci/check_junit.py /tmp/junit.xml --min-tests 1600
   python ci/check_test_db.py --database "$TEST_DB_NAME" --prefix ci_shop --baseline /tmp/baseline.json
   ```

   Шаг 9 (G9) локально не воспроизводится: `ci/g9_db.py prepare` намеренно отказывает вне GitHub-hosted runner. Семь тестов G9 локально выполняются только в лаборатории `digital_store_pr6_stage1_test` под ролью `pr6_stage1_owner`.

3. Удалить контейнер вместе с данными: `docker stop ds-ci-pg` (запущен с `--rm`).

`prepare_test_db.py` откажет, если:

- база не `*_test` или хост не локальный;
- роли `ci_shop_*` или база уже существуют.

Скрипт предназначен только для пустого одноразового кластера, а не для рабочего сервера.
