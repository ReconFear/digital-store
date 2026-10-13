# PR 3 — Payments & Security

> **rev2:** очередь уведомлений с арендой, запрет новой попытки при неразрешённой старой, проверка ответа на создание, сокращённые права — см. `PR3_NOTES_REV2.md`; восстановление найденного платежа со ссылкой и валюта возвратов — `PR3_NOTES_REV3.md`; ссылка при повторной сверке и запрет HTTP-перенаправлений — `PR3_NOTES_REV4.md`. Где описания ниже расходятся с rev2 (например, `stale_attempt`, права веб-роли, число тестов), действует rev2.

Основа — PR 2 rev3-fix (ветка `pr3-payments-security` от `877c132`). Миграции `0000`–`0002` не изменены (контрольные суммы проверяет тест). Проект не переписывался: добавлены модуль `payments/`, миграция `0003`, `http_security.py`, `deploy/db_roles.py`, новые маршруты v2 и методы JS-клиента.

**Главное.** Оплату учитывает только отдельный обработчик и только по состоянию платежа, которое он сам получил от ЮKassa (`GET /v3/payments/{id}`). Запись денежной операции, статус платежа, списание остатков, `paid` и задачи исполнения — одна транзакция. Публичный запрос (уведомление, checkout, статус) сделать заказ оплаченным не может; при раздельных ролях PostgreSQL этого не может и роль веб-приложения.

**По умолчанию платежи выключены.** Живой режим требует `YOOKASSA_MODE=live`, боевого ключа и `PAYMENTS_ALLOW_LIVE=I_UNDERSTAND_REAL_MONEY`. Публичный запуск — только после независимого аудита.

Подробно: `PAYMENTS.md` (архитектура, статусы, сбои, настройка), `DB_ROLES.md` (роли, пошаговый переход), `FRONTEND_API.md` (новые маршруты), `FRONTEND_INTEGRATION.md` §8а (для Артёма).

## 1. Что сделано по пунктам задания

| Пункт | Реализация |
|---|---|
| 1. Модуль с разделёнными обязанностями | `payments/provider.py` (интерфейс, ошибки), `yookassa.py` (HTTP-адаптер), `service.py` (создание платежа, учёт, уведомления, сверка, статус), `worker.py`, `access.py`. Списание остатков не продублировано: алгоритм `apply_verified_payment` перенесён без изменений в SQL-функцию `apply_paid_order`, Python-функция стала обёрткой. FakeProvider нет — в тестах настоящий адаптер ходит по HTTP в локальный имитатор |
| 2. Миграция 0003 | `order_access`, `payments`, `money_operations`, `provider_events`, `refunds`, `audit_log`; CHECK, FK, уникальные ключи (ключ идемпотентности, ID провайдера, денежная операция), частичные уникальные индексы «одна успешная оплата на заказ» и «одна активная попытка на заказ»; суммы `BIGINT` + валюта; триггеры неизменяемости, запрет `TRUNCATE`; `succeeded` невозможен без денежной операции на ту же сумму |
| 3. Настоящий провайдер | `POST /v3/payments` с `Idempotence-Key` (UUID v4, сохраняется в БД **до** запроса), `GET /v3/payments/{id}`; повторы только при неизвестном результате и только с тем же ключом; таймауты; 4xx — окончательный отказ; HTTP 200 на создание ≠ оплата; TLS ≥ 1.2 с проверкой сертификата; тестовый режим |
| 4. Webhooks | тело — только сигнал (тип + ID); 200 только после COMMIT в `provider_events`; сверка ID, заказа (метаданные из ответа провайдера), суммы, валюты, статуса, магазина (`recipient.account_id`), режима `test`; повторы/параллельность/порядок — безопасны; ошибки — 4xx/5xx, не фиктивный успех |
| 5. Денежная атомарность | `payment_record_provider_state` (одна транзакция); нехватка товара → платёж `succeeded`, деньги учтены, заказ `requires_action`, решение `admin_retry_paid_order`; сбой между провайдером и COMMIT → повтор с тем же ключом (checkout или сверка) |
| 6. Права PostgreSQL | `deploy/db_roles.py` (мигратор, приложение, обработчик, администратор), SECURITY DEFINER с путём `pg_catalog, <схема>, pg_temp`, `REVOKE EXECUTE FROM PUBLIC`, негативные тесты под настоящими ролями; ничего не применяется автоматически; `DB_ROLES.md` — пошагово |
| 7. HTTP API | лимиты частоты и размера (`http_security.py`); секрет доступа к заказу 256 бит (в БД только SHA-256), передаётся в теле; неверный секрет = несуществующий заказ; без PII и внутренних причин в ответах; v1/v2 и JS-клиент совместимы |
| 8. Статусы и возвраты | статус попытки, оплаты заказа, исполнения и возврата — раздельно; модель статусов заказа PR 2 не менялась; возвраты — только запросы (`requested`), исполнение через API не включено и не изображается |

## 2. Проверки — что реально выполнено

### Среда, где запускалось

Linux (песочница), Python 3.13.16, PostgreSQL 16.15, pytest 9.1.1, Node 22.22.0, Chromium (Playwright). **FastAPI и настоящий psycopg 3 недоступны** (PyPI закрыт): вместо psycopg — тестовая заглушка протокола PostgreSQL (не входит в проект), поэтому файлы тестов, которым нужен FastAPI, здесь не запускались. Тестовая база `digital_store_test` пересоздана через `migrate.py` под ролью `digital_store_tester` (без доступа к `digital_store`). Основная база `digital_store` не использовалась.

| Прогон | Результат |
|---|---|
| pytest без 7 файлов, которым нужен FastAPI | **399 passed, 33 skipped** (тесты прав без согласия) |
| то же с `DS_TEST_CREATE_ROLES=1` (временные роли, `CREATEROLE` выдан `digital_store_tester` в песочнице) | **432 passed** |
| из них прежних тестов PR 1–PR 2 | 243 — все проходят |
| база после прогонов | пустые `orders`/`payments`/`provider_events`, временных схем и ролей `dst_*` не осталось |
| JS-клиент в Node | **35 из 35** (было 30, +5) |
| витрина и JS-тесты в Chromium (stand-in backend PR 1.5) | **29 из 29** |
| Ruff (E, F, W, B) на новом коде | без замечаний |
| `api.py` импортирован с заглушкой FastAPI, обработчики маршрутов вызваны напрямую (заказ v2 → статус → checkout выключен → checkout с имитатором → уведомление с чужого IP 403 / с IP ЮKassa 200 → обработчик → `paid`) | работает (разовая проверка, не входит в набор тестов) |
| репетиция `DB_ROLES.md` на отдельной базе `ds_rehearsal_test`: база под `postgres` → 0003 → роли → передача владения → CONNECT → права → `migrate.py status` под мигратором → оплата под `app`/`worker` | работает: `app` получает `permission denied` на `apply_paid_order` и `payment_record_provider_state`, заказ оплачен обработчиком; база и роли удалены |

Контроль, что тесты ловят ошибки (мутации, затем откат):

| Мутация | Результат |
|---|---|
| в `payment_record_provider_state` убрана проверка суммы | `test_provider_amount_different_from_order_is_not_accepted_as_payment` падает |
| в `payment_attempt_start` убрана проверка секрета | `test_order_cannot_be_read_or_paid_without_its_secret` падает |
| роли приложения выдан `UPDATE` на `orders` целиком | 4 теста `test_db_privileges.py` падают |

### Не запускалось мной (нужен FastAPI)

42 теста: 33 прежних (`test_api.py`, `test_api_v2.py`, `test_cors.py`, `test_order_unavailable.py`, `test_order_validation.py`, `test_orders_access.py`) и **9 новых** `test_api_payments.py` (маршруты checkout/status/webhook через TestClient, `access_token` в ответе, 404 на чужой секрет, 422 на лишние поля, 500 при сбое базы в webhook, 429/413 на настоящем приложении). Ожидаемо у вас: **441 passed, 33 skipped**; с `DS_TEST_CREATE_ROLES=1` — **474 passed**.

## 3. Обязательные сценарии → тесты

| # | Сценарий | Тесты |
|---|---|---|
| 1 | Настоящий запрос с точной суммой | `test_payments_flow::test_checkout_sends_exact_order_amount_and_does_not_mark_paid`, `test_minor_units_are_converted_exactly_without_float`; `test_yookassa_adapter::test_create_payment_request_matches_protocol`, `test_amount_conversion_is_exact_both_ways` |
| 2 | Подмена суммы или валюты | `test_provider_amount_different_from_order_is_not_accepted_as_payment`, `test_unsupported_currency_from_provider_never_pays_order`, `test_currency_mismatch_in_verified_state_goes_to_requires_action`, `test_database_refuses_payment_attempt_with_amount_other_than_order_total`, `test_payment_amount_and_order_cannot_be_rewritten`; `test_migration_0003::test_payment_attempt_only_for_pending_orders_with_exact_total`; `test_api_payments::test_checkout_returns_provider_page_and_does_not_pay` (422 на `amount`) |
| 3 | Фальшивые webhooks и ложный `succeeded` | `test_fake_notification_for_unknown_payment_changes_nothing`, `test_false_succeeded_for_real_pending_payment_is_rechecked`, `test_foreign_succeeded_payment_is_ignored`, `test_payment_of_another_shop_is_not_accepted`, `test_metadata_pointing_to_another_order_is_not_accepted`, `test_live_payment_in_test_mode_and_vice_versa_is_not_accepted`, `test_malformed_notifications_are_rejected_not_faked` (×6), `test_notification_sender_ip_is_checked`, `test_notifications_are_refused_when_payments_disabled` |
| 4 | Два параллельных подтверждения | `test_parallel_confirmations_record_payment_once` (6 потоков), `test_concurrent_checkouts_create_one_attempt_and_one_payment` |
| 5 | Повторная доставка | `test_repeated_delivery_is_stored_once_and_applied_once` |
| 6 | Неправильный порядок | `test_late_events_after_success_do_not_change_anything`, `test_success_notification_before_local_binding_is_matched_by_provider_metadata`, `test_canceled_payment_allows_new_attempt` |
| 7 | Таймауты и повтор | `test_timeouts_leave_attempt_unknown_and_retry_reuses_key`, `test_lost_responses_never_create_second_payment`, `test_adapter_retries_transient_failures_within_one_call`, `test_worker_retries_event_when_provider_is_unavailable`, `test_provider_rejection_marks_attempt_failed_and_allows_new_one`; адаптер: `test_transient_failure_is_retried_with_same_key` (×6), `test_exhausted_retries_report_unknown_outcome`, `test_definite_failures_are_not_retried` (×3) |
| 8 | Ошибка БД после успешного ответа провайдера | `test_db_failure_after_provider_success_is_recovered_without_second_payment`, `test_db_failure_after_provider_success_is_recovered_by_next_checkout` |
| 9 | Конкурентная оплата при ограниченном остатке | `test_concurrent_paid_orders_with_limited_stock` (3 оплаченных заказа на 1 единицу) |
| 10 | Деньги приняты, товара нет | `test_money_received_but_stock_short_then_admin_resolves`, `test_admin_retry_requires_recorded_payment`, `test_payment_for_cancelled_order_is_recorded_with_refund_request`, `test_refund_request_cannot_exceed_received_money` |
| 11 | Несанкционированное изменение `orders.status` | `test_db_privileges::test_app_role_cannot_touch_money_status_or_history` (×21), `test_worker_role_has_no_direct_write_access` (×6), `test_admin_role_resolves_only_through_functions` (×3), `test_app_role_cannot_reopen_sealed_order`, `test_full_flow_under_separate_roles`; `test_migration_0003::test_financial_history_cannot_be_rewritten` (×9), `test_succeeded_requires_money_operation_even_for_table_owner` |
| 12 | Чтение чужого заказа | `test_order_cannot_be_read_or_paid_without_its_secret`, `test_token_is_stored_only_as_hash`, `test_v1_orders_have_no_token_and_cannot_be_paid`, `test_status_does_not_expose_internal_payment_details`; `test_api_payments::test_status_requires_the_order_secret`; `test_migration_0003::test_security_definer_ignores_temp_table_shadowing` |
| 13 | Rate limiting и размер | `test_http_security.py` (16), `test_api_payments::test_rate_limit_and_body_size_on_real_app` |
| 14 | Логи и секреты | `test_payment_flow_logs_contain_no_secrets`, `test_database_stores_no_card_data_or_provider_secrets`, `test_secret_never_appears_in_repr_errors_or_logs`, `test_config_errors_do_not_contain_the_secret`, `test_env_example_has_no_secrets_and_keeps_payments_disabled` |
| 15 | Миграция 0002 → 0003 с данными | `test_migration_0003::test_upgrade_from_0002_applies_only_0003_and_keeps_products_and_orders`, `test_after_upgrade_old_rules_still_hold`, `test_apply_paid_order_matches_python_behaviour_of_pr2` |
| 16 | Совместимость v1/v2 и JS | все прежние тесты (243 здесь + 33 у вас), JS 35/35, витрина 29/29, `test_v1_order_still_works_under_app_role`, `test_api_payments::test_v2_order_returns_access_token_once_and_order_shape_is_unchanged`, `test_old_routes_and_absence_of_order_reading_are_unchanged` |
| 17 | Восстановление после перезапуска | `test_event_accepted_before_restart_is_processed_by_new_worker`, `test_event_claimed_by_crashed_worker_is_retried_after_lease`, `test_reconcile_finds_paid_payment_without_any_notification`, `test_stale_unknown_attempt_goes_to_requires_action_and_late_money_is_recorded` |
| 18 | Повторное применение миграций, старые checksum | `test_old_migration_checksums_are_unchanged`, `test_reapplying_migrations_changes_nothing`, `test_new_migration_is_last_and_checksummed`; прежний `test_fresh_install_and_repeat_run` |

## 4. Что проверено только имитацией провайдера

**Всё взаимодействие с ЮKassa** проверено против `tests/yookassa_mock.py` — локального HTTP-сервера, повторяющего документированный протокол (Basic-авторизация, `Idempotence-Key`, тот же платёж на тот же ключ, 202/429/5xx, статусы). Адаптер при этом настоящий: те же `urllib`, заголовки и разбор ответа, что пойдут в `api.yookassa.ru`.

**Проверок на настоящем тестовом магазине ЮKassa не было** (учётных данных нет). До включения оплаты нужно проверить в тестовом магазине:

1. создание платежа и формат ответа (поля `confirmation.confirmation_url`, `recipient.account_id`, `test`, `metadata`);
2. **`recipient.account_id` равен shopId** — сверка магазина на этом построена; в документации поле описано как идентификатор магазина, но в песочнице это не подтверждено;
3. повтор с тем же `Idempotence-Key` возвращает тот же платёж; ответ 202 и поле `retry_after`;
4. доставка уведомлений на `/api/v2/payments/yookassa/webhook` через HTTPS и адреса отправителей;
5. требуется ли чек 54-ФЗ (`receipt`) для вашего магазина — если да, создание платежа будет отклонено (`ProviderRejected`), нужна доработка;
6. отмена неоплаченного платежа по истечении времени (`canceled`, `expired_on_confirmation`).

## 5. Не проверено на Windows / Python 3.14.3 / PostgreSQL 18

- весь набор на настоящем psycopg 3.3.6 (заглушка повторяет только используемое подмножество; отличия возможны в типах параметров вызовов функций и в `bytea`/`jsonb` — код рассчитан на оба варианта);
- 42 теста с FastAPI 0.143 / Starlette 1.7 (в песочнице Starlette 1.6 только для чистого ASGI);
- `CREATEROLE` в PostgreSQL 18 и вход новых ролей по паролю (`scram-sha-256`) в тестах прав;
- `urllib` + проверка сертификата на Windows (системное хранилище сертификатов) при обращении к `api.yookassa.ru`;
- формат `proconfig` в PostgreSQL 18 (`search_path=pg_catalog, public, pg_temp`).

## 6. Изменения прежних тестов (не ослабление — объяснение)

| Тест | Было | Стало | Почему |
|---|---|---|---|
| `test_order_integrity::test_cannot_truncate_history_even_with_cascade`, `test_truncate_is_refused_even_when_deletion_is_allowed` | `TRUNCATE order_items, orders, fulfillments` → сообщение содержит `TRUNCATE` | то же, сообщение `(?i)truncate`; во второй тест **добавлена** проверка `TRUNCATE orders CASCADE` | на `orders` теперь ссылаются таблицы 0003, и PostgreSQL отклоняет `TRUNCATE` без `CASCADE` раньше триггера: «cannot truncate a table referenced in a foreign key constraint». Запрет и неизменность данных проверяются как раньше |
| `test_order_integrity::test_trigger_functions_have_trusted_search_path_in_test_schema` | функции схемы == 10 функциям 0002 | функции схемы == 10 функций 0002 + 10 функций 0003, у **всех** доверенный путь | в схеме появились функции 0003; проверка стала шире |
| `test_migration_0002::test_only_0002_is_applied_to_a_0001_database` | применена ровно `["0002"]` | первой применена `0002`, затем только версии > `0001` по порядку и ровно те, что есть в папке | после 0002 теперь идёт 0003 |
| `test_frontend_contract::test_client_uses_only_existing_api_routes` | 6 путей | 8 путей (+ checkout, status) | новые методы клиента; проверка «пути клиента ⊆ маршруты api.py» сохранена |
| `conftest.py` | — | `HTTP_RATE_LIMITS=off`, `PAYMENTS_ENABLED=false` для прогона; уборка `make_product` удаляет и платёжные записи заказов | общие тесты API делают много запросов с одного адреса; ограничитель проверяется отдельно на своём экземпляре |

## 7. Ограничения — не считать готовой функциональностью

- **Возвраты не исполняются** через API ЮKassa. `refunds.status = 'requested'` — «нужно вернуть»; возврат делает человек в кабинете ЮKassa. Учёт выполненного возврата (`money_operations.kind = 'refund'`) не реализован.
- **Чеки 54-ФЗ** не передаются.
- **Двухстадийная оплата** не поддерживается (используется `capture: true`).
- **Резервирования нет**: оплаченный заказ при нехватке товара уходит в `requires_action`.
- **Заказы v1 и созданные до PR 3** оплатить нельзя (нет секрета доступа).
- **Ограничение частоты** — в памяти одного процесса; нужен лимит на обратном прокси.
- **Админ-интерфейса нет**: `admin_retry_paid_order`, `request_refund` и просмотр — SQL под ролью администратора.
- **Доверие к обработчику**: ЮKassa не подписывает уведомления; база гарантирует сумму, однократность и запись денег, но не может проверить, что обработчик действительно спросил провайдера. Компрометация сервера обработчика = возможность подделать учёт.
- **Отмена оплаченного заказа** с возвратом остатков не реализована.
- Единицы цен в основной базе `digital_store` по-прежнему **не подтверждены**; ×100 — рабочее допущение только для тестовой базы.

## 8. Проверка у вас (только тестовая база)

По одной команде в PowerShell из папки проекта:

```powershell
.\.venv\Scripts\python.exe migrate.py status --database digital_store_test
.\.venv\Scripts\python.exe migrate.py apply --database digital_store_test
.\.venv\Scripts\python.exe -m pytest tests -q
node frontend\tests\run-node.mjs
```

Ожидается: применена одна миграция `0003_payments_security.sql`; **441 passed, 33 skipped**; JS **35 из 35**.

Тесты прав (создают и удаляют временные роли `dst_*`; нужен `CREATEROLE` у тестовой роли — изменение роли выполняйте только осознанно, см. `DB_ROLES.md` §3):

```powershell
$env:DS_TEST_CREATE_ROLES = "1"
.\.venv\Scripts\python.exe -m pytest tests\test_db_privileges.py -q
Remove-Item Env:DS_TEST_CREATE_ROLES
```

Ожидается **34 passed**.

**`digital_store` не трогать**: ни миграцию 0003, ни роли, ни тестовые платежи — до отдельного решения.

## 9. Новые и изменённые файлы

| Файл | Что |
|---|---|
| `migrations/0003_payments_security.sql` | новая миграция |
| `schema.sql` | + объекты 0003 (снимок совпадает с миграциями — тест) |
| `payments/` | `__init__`, `provider`, `yookassa`, `config`, `access`, `service`, `worker` |
| `http_security.py`, `deploy/db_roles.py` | лимиты запросов; SQL ролей |
| `api.py` | middleware лимитов; `access_token` в ответе v2; маршруты checkout, status, webhook |
| `commerce/orders.py` | необязательный шаг `on_created` в транзакции создания заказа |
| `commerce/schemas.py` | схемы checkout/status, `access_token` |
| `payment_service.py` | обёртка над `apply_paid_order` (интерфейс прежний) |
| `frontend/lib/digital-store-api.js` | `checkoutV2`, `getOrderStatusV2`, `accessToken` из `createOrderV2`, коды ошибок оплаты |
| `frontend/tests/api-client.tests.js` | +5 тестов |
| `tests/` | `test_payments_flow.py` (54), `test_yookassa_adapter.py` (57), `test_migration_0003.py` (28), `test_http_security.py` (16), `test_db_privileges.py` (34), `test_api_payments.py` (9), `yookassa_mock.py`, `payment_testkit.py`; изменения — раздел 6 |
| `PAYMENTS.md`, `DB_ROLES.md`, `PR3_NOTES.md`, `.env.example` | новые |
| `README.md`, `FRONTEND_API.md`, `FRONTEND_INTEGRATION.md`, `COMMERCE_CORE.md` | обновлены |

Контрольные суммы применённых миграций (не изменились):

```text
0000_baseline.sql        76730405f960ab61b144f480a1cf531dd3642a3779d82d41e0892cd04d0cd411
0001_order_status.sql    68cb7222461b808cdc69eaa7ebe716a48c4d2cf7c2cc7b8e2229e3959a49f81b
0002_universal_core.sql  4c83941c9b8daae22c99d1f31961446d64665dfa972070a348429d3b35d96231
```

Новая миграция: `0003_payments_security.sql  e2e7767544d5cf9c652fe5bfc2ef08bc47d9432e6bf57cbfd7a7ac0ab909e209` (после применения — в `schema_migrations.checksum`).
