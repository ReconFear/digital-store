# CI (GitHub Actions)

Файл: `.github/workflows/ci.yml`. Запускается на Pull Request в `master`, на push в любые ветки, кроме `master`, и вручную (`workflow_dispatch`).

## Что делает

1. **Checkout** без сохранения токена в `.git/config`. Права workflow — только `contents: read`, секреты репозитория не используются.
2. **`git diff --check`** — изменения PR относительно базовой ветки. CR в конце строки не считается ошибкой: файлы проекта в CRLF.
3. **Нет `.env` и дампов** среди файлов репозитория (`.env.example` разрешён).
4. **Python 3.14**, зависимости из `requirements.txt`, проверка, что используется настоящий psycopg (бинарная реализация).
5. **PostgreSQL 18** — одноразовый service-контейнер. Скрипт `ci/prepare_test_db.py` готовит `digital_store_roles_test` по модели `DB_ROLES.md` штатными средствами:
   - роли `ci_shop_*` — `deploy/db_roles.py` с нестандартным префиксом. Мигратор без `SUPERUSER`, `app`, `worker`, `NOLOGIN`-группа `admin`;
   - `REVOKE CONNECT … FROM PUBLIC` и `CONNECT` ролям магазина (`database_sql`);
   - миграции 0000–0004 — `migrate.py apply` **под мигратором**. Владелец всех таблиц и `SECURITY DEFINER`-функций — мигратор;
   - права и `shop_security_settings.admin_group` (`grants_sql`), затем `seed.sql` под мигратором;
   - проверка модели безопасности и снимок прав (JSON без секретов).
6. **`python -m pytest -q -ra tests`** с `TEST_DB_NAME=DB_NAME=digital_store_roles_test`, `DS_TEST_CREATE_ROLES=1`, `DS_TEST_SUPERUSER=postgres`.
7. **`ci/check_junit.py`** — успешны все тесты, без `skip`/`xfail`/ошибок, тестов не меньше 780. Тесты прав PostgreSQL обязаны выполниться.
8. **`ci/check_test_db.py`** — выполняется **всегда**, даже после падения тестов. Падает, если:
   - нарушена модель безопасности: владельцы объектов, `SECURITY DEFINER` у суперпользователя или с `EXECUTE` у `PUBLIC`, `search_path`, `CONNECT`/`CREATE`/права на таблицы у `PUBLIC`, `admin_group`, журнал миграций с 0004 и контрольными суммами;
   - отличается от снимка до тестов хоть что-то из: роли и их атрибуты (например, остались `dst_t*`), членства, действующие права на базу, схемы, таблицы, столбцы и функции, права по умолчанию, состояние триггеров, `admin_group`, миграции, временные схемы.

## Учётные данные

Пароль суперпользователя в `ci.yml` (`ci-disposable-postgres`) — публичное значение одноразового контейнера, который существует только во время job на раннере GitHub. Это не секрет и не пароль какой-либо настоящей базы. Пароль мигратора генерируется случайно внутри `prepare_test_db.py`, никуда не выводится и не сохраняется. У ролей `app`/`worker` паролей нет: тесты прав создают свои временные роли со случайными паролями и удаляют их.

Тесты подключаются суперпользователем контейнера. Он нужен тестам прав, чтобы создавать временные роли, выдавать им `CONNECT` и членство в группе администраторов. Так же они проверялись на Windows. Предохранитель `tests/conftest.py` разрешает подключение только к `TEST_DB_NAME`.

## Локальный запуск тех же проверок

```bash
# пустой одноразовый кластер PostgreSQL 16+, суперпользователь в DB_USER/DB_PASSWORD
export DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_PASSWORD=...
python ci/prepare_test_db.py --database digital_store_roles_test --prefix ci_shop --baseline /tmp/baseline.json
TEST_DB_NAME=digital_store_roles_test DB_NAME=digital_store_roles_test DS_TEST_CREATE_ROLES=1 DS_TEST_SUPERUSER=postgres \
  DS_TEST_SUPERUSER_PASSWORD="$DB_PASSWORD" python -m pytest -q -ra tests --junitxml=/tmp/junit.xml
python ci/check_junit.py /tmp/junit.xml --min-tests 780
python ci/check_test_db.py --database digital_store_roles_test --prefix ci_shop --baseline /tmp/baseline.json
```

`prepare_test_db.py` откажет, если:

- база не `*_test` или хост не локальный;
- роли `ci_shop_*` или база уже существуют.

Скрипт предназначен только для пустого одноразового кластера, а не для рабочего сервера.
