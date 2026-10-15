# Digital Store — раздельные роли PostgreSQL (PR 3, дополнено в PR5)

Цель: приложение не может само сделать заказ оплаченным, изменить остатки, статусы и финансовую историю. Это закрывает риск PR 2 «приложение с широкими правами вручную переводит заказ в `paid`».

**Ничего из этого не применяется автоматически.** `deploy/db_roles.py` только печатает SQL. Существующие роли (`postgres`, `digital_store_tester` и др.) не изменяются. Основную базу `digital_store` не трогать до отдельного согласования — сначала отрепетировать на отдельной тестовой базе (шаг 0).

## 1. Роли

| Роль | Вход | Назначение | Может | Не может |
|---|---|---|---|---|
| `digital_store_migrator` | да | `migrate.py` | владелец таблиц и функций магазина | — (используется только для миграций) |
| `digital_store_app` | да | `uvicorn api:app` | читать каталог (`products`, продаваемый остаток `product_sellable_stock`) и заказы (`orders`, `order_items` — нужно для `RETURNING` и проверок 0002), создавать заказы; через функции **с секретом заказа** — выдать секрет, посмотреть статус, начать оплату, привязать/пометить СВОЮ попытку, получить шифротексты СВОИХ выданных ключей (`order_delivery_view`, PR5); положить уведомление в очередь | читать `order_access` (хэши секретов), `payments`, `refunds`, `fulfillments`, `provider_events`, `money_operations`, `audit_log`, таблицы ключей PR5; `UPDATE` статусов, цен, остатков; `DELETE`/`TRUNCATE`; трогать попытки чужих заказов; вызывать `apply_paid_order`, `payment_record_provider_state`, `payment_attempt_bind/mark/touch`, админ-функции; создавать объекты |
| `digital_store_worker` | да | `python -m payments.worker`, `python -m commerce.fulfillment_worker` | читать `payments`, `provider_events`, заказы, товары, задачи исполнения; учитывать проверенную оплату (`payment_record_provider_state`); привязывать/помечать попытки при сверке; обновлять аренду и статус событий и задачи исполнения (только перечисленные столбцы); назначать ключи задаче (`digital_keys_assign`, PR5) | читать `order_access`, `money_operations`, таблицы ключей; возобновлять/отменять задачи и ставить `fulfilled` без ключей (триггер 0004); прямой `UPDATE` заказов, товаров, платежей; `INSERT`/`DELETE` событий; `apply_paid_order` и функции веб-роли напрямую |
| `digital_store_admin` | нет (группа) | люди — только через персональные учётные записи (`--part admin-login`) | читать всё, кроме хэшей секретов заказов и шифротекстов/отпечатков ключей; `admin_retry_paid_order`, `request_refund`, `admin_resolve_payment`; управление ключами PR5 (`admin_*`, `DIGITAL_KEYS.md`, раздел 5) | прямой `UPDATE`/`DELETE`; `apply_paid_order` напрямую |

Финансовые изменения — только через функции `SECURITY DEFINER` миграции 0003: владелец — мигратор (не суперпользователь), путь поиска закреплён (`pg_catalog, <схема>, pg_temp`), `EXECUTE` у `PUBLIC` отозван. Триггеры 0002/0003 дополнительно защищают историю даже от владельца (кроме `ALTER TABLE ... DISABLE TRIGGER` — это право владельца, поэтому мигратор не используется приложением).

## 2. Пошаговый переход

Команды — PowerShell из папки проекта. `psql` — `& "C:\Program Files\PostgreSQL\18\bin\psql.exe"`. Пароли **не** пишутся в файлы и в историю команд: задаются интерактивно через `\password`.

### Шаг 0. Репетиция на отдельной базе

```powershell
$psql = "C:\Program Files\PostgreSQL\18\bin\psql.exe"
& $psql -U postgres -c "CREATE DATABASE digital_store_roles_test"
$env:DB_USER = "postgres"   # только на время репетиции; пароль спросит psql/драйвер
.\.venv\Scripts\python.exe migrate.py apply --database digital_store_roles_test --confirm-database digital_store_roles_test
& $psql -U postgres -d digital_store_roles_test -f seed.sql
```

Дальше шаги 1–6 выполняются с `-d digital_store_roles_test`. Только после успешной репетиции и отдельного решения — то же для рабочей базы.

### Шаг 1. Резервная копия

```powershell
& "C:\Program Files\PostgreSQL\18\bin\pg_dump.exe" -U postgres -Fc -f backup_before_roles.dump digital_store_roles_test
```

Файл дампа не класть в Git и архивы проекта.

### Шаг 2. Создать роли (один раз на сервер PostgreSQL)

```powershell
.\.venv\Scripts\python.exe deploy\db_roles.py --part roles > roles_1_create.sql
notepad roles_1_create.sql          # прочитать
& $psql -U postgres -d digital_store_roles_test -v ON_ERROR_STOP=1 -f roles_1_create.sql
& $psql -U postgres -d digital_store_roles_test
#   \password digital_store_migrator
#   \password digital_store_app
#   \password digital_store_worker
#   GRANT digital_store_admin TO <ваша_личная_роль>;   -- по необходимости
#   \q
```

Если роли с такими именами уже есть — скрипт остановится с ошибкой; выберите другой префикс (`--prefix shop1`) во всех шагах.

### Шаг 3. Передать объекты магазина мигратору

```powershell
.\.venv\Scripts\python.exe deploy\db_roles.py --part ownership > roles_2_owner.sql
& $psql -U postgres -d digital_store_roles_test -v ON_ERROR_STOP=1 -f roles_2_owner.sql
```

Меняется владелец только таблиц и функций магазина (явные списки), не `REASSIGN OWNED`. Последовательности переходят вместе с таблицами.

### Шаг 4. Подключение к базе только ролям магазина

```powershell
.\.venv\Scripts\python.exe deploy\db_roles.py --part database --database digital_store_roles_test > roles_3_connect.sql
& $psql -U postgres -d digital_store_roles_test -v ON_ERROR_STOP=1 -f roles_3_connect.sql
```

`REVOKE CONNECT ... FROM PUBLIC`: другие роли (кроме владельца базы и суперпользователей) больше не подключатся. Если к этой базе ходит ещё кто-то — выдайте ему `CONNECT` явно.

### Шаг 5. Права

```powershell
.\.venv\Scripts\python.exe deploy\db_roles.py --part grants > roles_4_grants.sql
& $psql -U postgres -d digital_store_roles_test -v ON_ERROR_STOP=1 -f roles_4_grants.sql
```

**После каждой новой миграции** шаг 5 нужно повторить (и дополнить `db_roles.py` для новых таблиц).

PR5: вывод `--part grants` в конце содержит `INSERT INTO <схема>.shop_security_settings …` — какая группа считается администраторами магазина (проверяется административными функциями 0004 в базе). Выполнять **после** миграции 0004 (иначе таблицы ещё нет). При другом префиксе ролей (`--prefix shop1`) там будет `shop1_admin`.

### Шаг 5а (PR5). Персональные учётные записи администраторов

```powershell
.\.venv\Scripts\python.exe deploy\db_roles.py --part admin-login --login ivan_admin > roles_5_admin.sql
& $psql -U postgres -d digital_store_roles_test -v ON_ERROR_STOP=1 -f roles_5_admin.sql
& $psql -U postgres -d digital_store_roles_test -c "\password ivan_admin"
```

`GRANT <prefix>_admin TO ivan_admin WITH INHERIT TRUE, SET FALSE` (PostgreSQL 16+): права группы действуют сразу, `SET ROLE` в группу запрещён. Мигратор и суперпользователь административные функции PR5 вызвать не могут — аварийный порядок в `DIGITAL_KEYS.md`, раздел 5.

### Шаг 6. Переключить процессы

- `.env` веб-приложения: `DB_USER=digital_store_app`, `DB_PASSWORD=<пароль app>`.
- Обработчик платежей — отдельный `.env` или переменные окружения процесса: `DB_USER=digital_store_worker`.
- Worker выдачи (PR5) — та же роль `digital_store_worker`, `FULFILLMENT_MODULES=digital_keys`, **без** `DIGITAL_KEYS_*` (`DIGITAL_KEYS.md`, раздел 4).
- CLI ключей (PR5) — `ADMIN_DB_USER=<персональная роль>`, пароль спрашивается.
- Миграции: `$env:DB_USER = "digital_store_migrator"` перед `migrate.py apply`.

Проверка:

```powershell
$env:DB_USER = "digital_store_app"
& $psql -U digital_store_app -d digital_store_roles_test -c "UPDATE orders SET status = 'paid' WHERE id = 1"
#   ожидается: ERROR: permission denied for table orders
& $psql -U digital_store_app -d digital_store_roles_test -c "SELECT apply_paid_order(1)"
#   ожидается: ERROR: permission denied for function apply_paid_order
```

### Откат

```sql
-- под postgres, в той же базе
REASSIGN OWNED BY digital_store_migrator TO postgres;   -- мигратор владеет только объектами магазина
GRANT CONNECT ON DATABASE digital_store_roles_test TO PUBLIC;
```

и вернуть прежний `DB_USER` в `.env`. Либо восстановить дамп шага 1 (`pg_restore --no-owner`).

## 3. Автоматическая проверка прав

`tests/test_db_privileges.py` (55 тестов) и `tests/test_digital_keys_privileges.py` (PR5) создают **временные** роли `dst_<случайно>_*` во временной схеме, проверяет запреты и полный процесс оплаты под ролями, затем удаляет роли. Так как это изменение кластера (новые роли), тесты запускаются только по явному согласию:

```powershell
$env:DS_TEST_CREATE_ROLES = "1"
.\.venv\Scripts\python.exe -m pytest tests\test_db_privileges.py -v
Remove-Item Env:DS_TEST_CREATE_ROLES
```

Тестовой роли нужно право `CREATEROLE` (`ALTER ROLE digital_store_tester CREATEROLE;` под `postgres` — это изменение существующей роли, выполнять только осознанно). Без согласия или без права тесты пропускаются (`skipped`) с объяснением. Новые роли подключаются по паролю (случайному, создаётся тестом) — проверьте, что `pg_hba.conf` разрешает парольный вход на `localhost` (по умолчанию в установщике Windows — да, `scram-sha-256`).

## 4. Что права не решают

- Права ограничивают **базу**. Роль обработчика доверяет ответу провайдера, который получает код обработчика: компрометация сервера с обработчиком позволяет подделать учёт. Поэтому обработчик — отдельный процесс с отдельными учётными данными.
- Ключ ЮKassa хранится в окружении веб-процесса и обработчика; доступ к серверу = доступ к ключу.
- Суперпользователь и мигратор могут всё (в том числе отключить триггеры). Их пароли не должны быть в `.env` приложения.
- Секрет шифрования ключей (PR5) — у веб-процесса: компрометация веб-сервера раскрывает ключи товара, которые он может расшифровать. Права базы этого не закрывают (`DIGITAL_KEYS.md`, раздел 2).
