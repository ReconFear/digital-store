# PR 3 rev3 — продолжение найденного платежа и валюта возвратов

Основа — `digital-store-pr3-rev2.zip`. Миграции `0000`–`0002` не изменены ни на байт (сверено с архивом PR 3). Изменена `0003` (на Windows ещё не применялась), `schema.sql` пересобран. Аренда событий с `lease_token`, блокировка неразрешённых попыток, ограничения ролей и остальные исправления rev2 сохранены. Основная база `digital_store` не использовалась.

## 1. Найденный у провайдера неоплаченный платёж — исправлено

**Было.** «Потерянная» попытка (`unresolved_attempt`), найденная в списке ЮKassa в статусе `pending`, привязывалась без `confirmation_url`. После этого checkout отвечал `awaiting_payment` без ссылки, и оплатить было нечем.

**Стало** (`payments/service.py` — `_apply_provider_payment`, `_provider_state_problem`; `0003` — `payment_attempt_bind`, `payment_attempt_touch`):

1. До привязки обработчик проверяет состояние, полученное от ЮKassa:
   - metadata (заказ и попытка);
   - `recipient.account_id` = shopId;
   - согласованность `status` и `paid`;
   - для неоплаченного платежа — ещё сумма, валюта и режим `test`. У оплаченного расхождение суммы и валюты по-прежнему учитывается как полученные деньги с запросом возврата.
2. Ссылка передаётся в базу, только если проверки пройдены, статус `pending`, а ссылка — `https://` без логина и порта (кроме 443) на домене из `YOOKASSA_CONFIRMATION_HOSTS`. База сохраняет её при поздней привязке только для `unresolved_attempt` без решения администратора.
3. Подходящая ссылка: попытка возвращается в `pending`, checkout отдаёт покупателю **ту же** ссылку на **тот же** платёж. Новой попытки и нового платежа у провайдера нет.
4. Ссылки нет или она непригодна: попытка честно остаётся `requires_action` / `unresolved_attempt`. Checkout отвечает 409 `payment_under_review`, статус для покупателя — `requires_action`, одна запись `payment.unresolved_checked` в журнале. Сверка каждые 5 минут проверяет этот платёж через `GET /v3/payments/{id}`. Когда ЮKassa отменит его — `canceled`, оплата открывается. Если его всё же оплатят — обычный учёт, один раз.
5. Расхождение суммы, валюты, магазина, режима или `status`/`paid`: `requires_action` с причиной, ссылки нет, денег не учтено, новые попытки заблокированы до решения человека.

## 2. Валюта возврата — исправлено

**Найдено при проверке путей создания `refunds`.**

- Автоматические запросы возврата в `payment_record_provider_state` уже брали сумму и валюту от провайдера: `currency_mismatch`, `amount_mismatch`, `duplicate_payment`, `order_not_payable`, поздние деньги.
- `request_refund` брал валюту из **платежа (= заказа)** и сравнивал суммы без учёта валюты. Это ошибка.
- Кроме того, при `currency_mismatch` в валюте, отличной от RUB, учёт вообще не доходил до базы: адаптер разбирал суммы только в валютах продажи и падал с ошибкой протокола.

**Стало:**

- `request_refund` — сумма и валюта по `money_operations` этого платежа; лимит — полученная сумма минус неотменённые запросы; в журнал пишется и валюта.
- Новый триггер `refunds_guard` проверяет **все** пути создания возврата:
  - деньги по платежу учтены;
  - валюта и заказ совпадают с учтённой оплатой;
  - сумма неотменённых запросов не больше полученного;
  - платёж, заказ, сумма, валюта и ключ запроса неизменяемы;
  - «воскресить» отменённый запрос сверх лимита нельзя;
  - параллельные запросы сериализуются блокировкой строки платежа.
- Адаптер принимает сумму от провайдера в известных валютах ISO 4217 (`PROVIDER_CURRENCY_EXPONENTS`: RUB, USD, EUR, BYN, KZT и др.). Продаются по-прежнему только валюты из `commerce/money.py`. Неизвестная валюта — ошибка протокола, учёта нет.
- Возврат по-прежнему **не исполняется** автоматически: запись `requested`, деньги возвращает человек в кабинете ЮKassa. Операций `money_operations.kind = 'refund'` не появляется.

## 3. Тесты

### Где и как запускалось

- Окружение: Linux (песочница), **настоящий PostgreSQL 16.15**, Python 3.13.16, pytest 9.1.1, Node 22.22.
- ЮKassa — **только локальный HTTP-имитатор** протокола (`tests/yookassa_mock.py`). Настоящий адаптер ходит в него по HTTP.
- Вместо psycopg — тестовая заглушка протокола PostgreSQL (вне проекта). FastAPI недоступен.
- `digital_store_test` пересоздана через `migrate.py` под `digital_store_tester`.

| Прогон | Результат |
|---|---|
| pytest без 7 файлов, которым нужен FastAPI | **461 passed, 54 skipped** (тесты прав без согласия) |
| то же с `DS_TEST_CREATE_ROLES=1` | **515 passed** |
| из них прежние PR 1 – PR 2 | 243 — все проходят |
| JS-клиент | **35 из 35** |
| Ruff (E, F, W, B) | без замечаний |
| `api.py` с заглушкой FastAPI (обработчики напрямую) | работает |
| база, роли `dst_*`, временные схемы после прогонов | чисто |

### Новые тесты — `tests/test_payments_rev3.py` (18)

| Тест | Что проверяет |
|---|---|
| `test_lost_response_after_24h_pending_payment_is_recovered_with_link_and_paid_once` | **сквозной**: ответ на создание потерян → прошли сутки → checkout 409 → сверка находит платёж `pending` → ссылка восстановлена → checkout отдаёт ту же ссылку, нового платежа нет → оплата → уведомление → `paid`, остаток списан **один раз** (5 → 3), одна денежная операция, одна задача исполнения, возвратов нет; повторное уведомление ничего не списывает |
| `test_recovered_pending_without_valid_link_is_honest_requires_action` ×4 | нет `confirmation`, `http://`, чужой домен, нет `confirmation_url`: `requires_action`, 409, ссылка `null`, одна запись в журнале при двух сверках; после отмены у провайдера — `canceled` и новая оплата |
| `test_recovered_link_is_kept_only_after_lost_payment_is_paid_by_buyer_who_has_it` | найден без ссылки, затем оплачен — обычный учёт, заказ `paid` |
| `test_recovered_pending_payment_is_checked_before_link_is_stored` ×6 | сумма, валюта, магазин, режим, `status`/`paid`, чужой заказ в metadata: ссылка не сохраняется, денег нет, 409 |
| `test_money_in_other_currency_is_recorded_and_refund_is_requested_in_that_currency` ×2 | заказ в RUB, получено 100.00 USD (`currency_mismatch`) и 12.34 USD (`amount_mismatch`): деньги и автоматический запрос возврата — в USD на полученную сумму, только `requested` |
| `test_manual_refund_request_uses_received_currency_and_amount` | `request_refund` в USD, частями, не больше полученного; валюта заказа (RUB) не используется |
| `test_every_refund_path_is_bound_to_received_money` | прямой `INSERT` в RUB и сверх суммы, изменение суммы и валюты, «воскрешение» отменённого — отклонены |
| `test_refund_requires_recorded_money` | без учтённых денег возврат не создаётся |
| `test_unknown_currency_is_still_never_recorded` | неизвестная валюта: учёта нет, повтор |

### Мутационные проверки (ошибка внесена → тест упал → откат)

| Мутация | Результат |
|---|---|
| поздняя привязка не сохраняет ссылку (поведение rev2) | сквозной тест падает |
| `request_refund` в валюте заказа и без `refunds_guard` | 3 теста возвратов падают |
| ссылка из найденного платежа без проверки https и домена | 2 теста падают |
| у неоплаченного найденного платежа не проверяется сумма | 1 тест падает |

### Изменённые тесты

| Тест | Что и почему |
|---|---|
| `test_payments_flow::test_unsupported_currency_from_provider_never_pays_order` | валюта ответа `USD` → `XXX`. В rev3 USD — известная валюта и учитывается как `currency_mismatch` (новые тесты выше). Смысл теста сохранён: неизвестная валюта не оплачивает заказ, событие повторяется с `ProviderProtocolError` |
| `test_db_privileges` | `payment_attempt_touch` получил второй параметр (`note`) |
| `test_order_integrity`, `deploy/db_roles.py` | в список функций 0003 добавлен `refunds_guard` (проверка «у всех функций доверенный путь» сохранена) |

## 4. Имитатор и настоящая ЮKassa

| Проверено | Чем |
|---|---|
| весь платёжный процесс, поиск по списку, восстановление ссылки, валюты в ответах | **только локальный имитатор** |
| база, триггеры, функции, права ролей, конкурентность | настоящий PostgreSQL 16.15 |
| **настоящий тестовый магазин ЮKassa** | **не проверялся** — sandbox-платежей не было |

Перед включением оплаты нужно проверить в тестовом магазине:

- всё из `PR3_NOTES.md` §4 и `PR3_NOTES_REV2.md` §6;
- что элементы списка `GET /v3/payments` содержат `confirmation.confirmation_url` у платежа `pending` (иначе найденный платёж будет ждать отмены, а не продолжаться);
- с каких доменов ЮKassa отдаёт ссылки оплаты (по умолчанию разрешены `yoomoney.ru`, `yookassa.ru`).

## 5. Не проверено

- 43 теста с FastAPI. Ожидаемо у тебя: **504 passed, 54 skipped**; с `DS_TEST_CREATE_ROLES=1` — **558 passed**.
- Windows / Python 3.14.3 / PostgreSQL 18 / настоящий psycopg 3.3.6.

## 6. Проверка у тебя (только `digital_store_test`)

Если в тестовую базу уже применялась 0003 из rev1 или rev2 — пересоздай её: контрольная сумма 0003 изменилась.

```powershell
.\.venv\Scripts\python.exe migrate.py apply --database digital_store_test
.\.venv\Scripts\python.exe -m pytest tests -q
node frontend\tests\run-node.mjs
```

Ожидается: применена `0003_payments_security.sql`; **504 passed, 54 skipped**; JS **35 из 35**.

Контрольные суммы:

```text
0000_baseline.sql            76730405f960ab61b144f480a1cf531dd3642a3779d82d41e0892cd04d0cd411  (без изменений)
0001_order_status.sql        68cb7222461b808cdc69eaa7ebe716a48c4d2cf7c2cc7b8e2229e3959a49f81b  (без изменений)
0002_universal_core.sql      4c83941c9b8daae22c99d1f31961446d64665dfa972070a348429d3b35d96231  (без изменений)
0003_payments_security.sql   fd7aa44a5badc785f25b186f130a0cfb341780233a8793d61a0f6559523f56ef
```

## 7. Изменённые файлы

- `migrations/0003_payments_security.sql`, `schema.sql`;
- `payments/service.py`, `payments/provider.py`;
- `deploy/db_roles.py`;
- `tests/test_payments_rev3.py` (новый), `tests/test_payments_flow.py`, `tests/test_db_privileges.py`, `tests/test_order_integrity.py`;
- `PAYMENTS.md`, `PR3_NOTES.md` (пометка), `PR3_NOTES_REV3.md`.
