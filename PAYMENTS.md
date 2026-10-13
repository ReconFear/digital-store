# Digital Store — платежи (PR 3)

Платёжная подсистема создаёт платёж у настоящего провайдера (ЮKassa), учитывает оплату **только** по состоянию, которое сервер сам получил от провайдера, и проводит заказ в одной транзакции с записью денежной операции.

**По умолчанию платежи выключены** (`PAYMENTS_ENABLED=false`). Живые платежи требуют двух явных настроек (см. «Настройка»). Публичный запуск — только после независимого аудита.

## 1. Компоненты

```text
payments/
├── provider.py   # интерфейс провайдера, типы ошибок, точный перевод копеек в "123.45"
├── yookassa.py   # адаптер ЮKassa API v3: настоящий HTTP (urllib), ключи идемпотентности, повторы
├── config.py     # настройки из окружения; живой режим только по явному подтверждению
├── access.py     # секрет доступа к заказу (выдача и проверка по SHA-256)
├── service.py    # checkout, статус заказа, приём уведомлений, обработка очереди, сверка
├── worker.py     # процесс-обработчик: python -m payments.worker --once | --interval N
└── __init__.py   # build_payment_service() — сборка по настройкам
http_security.py  # ограничение частоты и размера запросов (ASGI)
deploy/db_roles.py  # SQL раздельных ролей PostgreSQL (печатает, не применяет)
migrations/0003_payments_security.sql
```

Заменить провайдера = написать класс с методами `create_payment(...)` и `get_payment(id)`, возвращающий `ProviderPayment`, и те же типы ошибок (`ProviderUnavailable` — результат неизвестен; `ProviderRejected`/`ProviderAuthError` — точно отказ; `ProviderNotFound`; `ProviderProtocolError`). Остальной код от провайдера не зависит.

## 2. Кто что может (два процесса, две роли БД)

| | Веб-процесс `uvicorn api:app` (роль `*_app`) | Обработчик `python -m payments.worker` (роль `*_worker`) |
|---|---|---|
| создать заказ, выдать секрет | да | нет |
| начать оплату, получить ссылку | да — `payment_attempt_start`, `payment_checkout_bind`, `payment_checkout_mark`, **только с секретом этого заказа** | да (сверка: `payment_attempt_bind`, `_mark`, `_touch`) |
| посмотреть статус заказа | да — `order_status_view`, только с секретом | — |
| принять уведомление | да — `provider_event_accept` (только запись в очередь) | — |
| запросить платёж у провайдера и **учесть оплату** | **нет** (нет `EXECUTE` на `payment_record_provider_state`) | да |
| читать `order_access`, `payments`, `refunds`, `fulfillments`, `provider_events` | **нет** (rev2) | `payments`, `provider_events`, `fulfillments` |
| изменить `orders.status`, остатки, платежи напрямую | нет | нет |

Даже полностью скомпрометированный веб-процесс (например, через SQL-инъекцию) не может сделать заказ оплаченным, не видит хэши секретов заказов и не может привязать или пометить платёжную попытку чужого заказа: функции веб-роли требуют SHA-256 секрета того заказа, к которому относится попытка (см. `DB_ROLES.md`, тесты `tests/test_db_privileges.py`).

## 3. Процесс оплаты

```text
покупатель                 веб-процесс                       PostgreSQL                         ЮKassa
POST /api/v2/orders  ───▶  create_order_with_access  ───▶  orders, order_items,
                           ◀── { order, access_token }      order_access(SHA-256 секрета)
POST .../{id}/checkout ─▶  payment_attempt_start  ─────▶  payments: 'creating',
  { access_token }                                         ключ идемпотентности (UUID v4) COMMIT
                           create_payment(ключ, сумма заказа) ───────────────────────────▶  POST /v3/payments
                           ◀──────────────────────────────────────────────────────────── 200 {id, pending, confirmation_url}
                           payment_attempt_bind ────────▶  payments: 'pending', provider_payment_id
                           ◀── { confirmation_url }
(оплата на стороне ЮKassa)
                                                                                         ◀── уведомление
POST .../yookassa/webhook  accept_notification ────────▶  provider_events: 'received'  COMMIT ─▶ 200
обработчик                 process_due_events ─────────────────────────────────────────▶  GET /v3/payments/{id}
                           проверки (ниже) ───────────▶  payment_record_provider_state:
                                                           money_operations + payments 'succeeded'
                                                           + apply_paid_order (остатки, paid,
                                                           fulfillments) — ОДНА транзакция
```

### Что проверяется перед учётом оплаты

Тело уведомления используется только для выбора, *что* перепроверить (тип события и ID). Затем обработчик сам запрашивает `GET /v3/payments/{id}` и проверяет:

| Проверка | Где | Результат при несовпадении |
|---|---|---|
| ID платежа у провайдера = ID в ответе = ID попытки | `service.py`, функция БД | ошибка протокола / отказ |
| попытка существует (по ID провайдера или по `metadata.payment_attempt` из ответа провайдера) | `service.py` | событие `ignored` (`unknown_payment`) |
| `metadata.order_id` и `metadata.payment_attempt` совпадают с попыткой | `service.py` | платёж `requires_action` (`metadata_mismatch`) |
| `recipient.account_id` = `YOOKASSA_SHOP_ID` (идентичность магазина) | `service.py` | `requires_action` (`shop_mismatch`) |
| `status` согласован с `paid` (pending/canceled — `false`, waiting_for_capture/succeeded — `true`) | `service.py` | `requires_action` (`provider_state_inconsistent`) |
| сумма = сумма попытки (= итог заказа, неизменяемый) | функция БД | `requires_action` (`amount_mismatch`) + деньги учтены + запрос возврата |
| валюта = валюта заказа | функция БД (неподдерживаемая валюта — ещё в адаптере) | `requires_action` (`currency_mismatch`) + возврат |
| `test` совпадает с режимом магазина | функция БД | `requires_action` (`test_mode_mismatch`) |
| статус `succeeded` (конечный) | функция БД | `pending`/`waiting_for_capture` — ничего не проводится |
| у заказа ещё нет учтённой оплаты | функция БД (+ уникальный индекс) | `requires_action` (`duplicate_payment`) + возврат |

### Проверка ответа на создание платежа (до выдачи ссылки)

Ссылка `confirmation_url` отдаётся покупателю, только если ответ на `POST /v3/payments` согласован: тот же `metadata` (заказ и попытка), сумма и валюта попытки, `recipient.account_id` = shopId, `test` = режим магазина, `status` согласован с `paid`, а для `pending` — ссылка `https://` без учётных данных и порта (кроме 443) на домене из `YOOKASSA_CONFIRMATION_HOSTS`. Если ЮKassa на тот же ключ вернула платёж с другим ID, чем уже привязан к попытке, — ссылки тоже нет (`payment.bind_id_conflict` в журнале). Любое расхождение — `requires_action` / `create_response_mismatch:<metadata|amount|shop|test_mode|status_paid|confirmation_url>`.

## 4. Статусы (разделены)

**Платёжная попытка** (`payments.status`): `creating` → `unknown` → `pending` → `waiting_for_capture` → `succeeded` | `canceled` | `failed` | `requires_action`. Конечные: `succeeded`, `canceled`, `failed`, `requires_action`. Переходы охраняет триггер `payments_guard`; `succeeded` невозможен без записи в `money_operations` на ту же сумму (даже для владельца таблиц).

`requires_action` без решения (`resolved_at IS NULL`) **блокирует новые попытки оплаты заказа** (`payment_under_review`, HTTP 409): пока не исключено, что по старой попытке придут деньги, вторую ссылку покупатель не получит. Решение принимает администратор: `admin_resolve_payment(id, 'not_paid_confirmed' | 'handled_manually', 'основание')`. Единственное исключение из «конечности» — `requires_action` с причиной `unresolved_attempt` (см. раздел 5а): если такой платёж найдётся у провайдера, он обрабатывается как обычный.

**Оплата заказа** (`orders.status`, модель PR 2 без изменений): `pending` → `paid` | `requires_action` | `cancelled`; `requires_action` → `paid` | `cancelled`; `legacy` — исторические. Переходов не добавлено.

**Исполнение** (`fulfillments.status`, PR 2): `pending`, `in_progress`, `fulfilled`, `requires_action`, `failed`, `cancelled`.

**Возврат** (`refunds.status`): `requested` → `pending` → `succeeded` | `canceled` | `requires_action`. **В PR 3 возврат у провайдера не исполняется**: запись `requested` означает «нужно вернуть деньги», её обрабатывает человек в личном кабинете ЮKassa. Статус сам не меняется и не изображает выполненный возврат.

Сумма и валюта возврата — **по фактически полученным деньгам** (`money_operations`), а не по заказу: если при `currency_mismatch` деньги пришли в долларах, запрос возврата тоже в долларах. Это проверяет триггер `refunds_guard` для всех путей (автоматические запросы и `request_refund`): валюта и заказ совпадают с учтённой оплатой, сумма неотменённых запросов не больше полученного, сумма и валюта запроса неизменяемы, отменённый запрос нельзя «воскресить» сверх лимита. Провайдер может сообщить деньги в любой валюте из списка `PROVIDER_CURRENCY_EXPONENTS` (`payments/provider.py`); неизвестная валюта — ошибка протокола, учёт не делается, уведомление повторяется.

Покупателю (`POST /api/v2/orders/{id}/status`) показываются обобщённые значения без внутренних причин: `payment_status` (`none`, `awaiting_payment`, `succeeded`, `canceled`, `failed`, `requires_action`), `fulfillment_status`, `refund_status`.

## 5. Сбои и повторы

| Ситуация | Что происходит |
|---|---|
| Таймаут/обрыв/5xx/429/202 при создании платежа | адаптер повторяет (до 3 раз) **с тем же ключом**; если не вышло — попытка `unknown`, покупателю 503. Следующий checkout или сверка повторит с тем же ключом: ЮKassa вернёт тот же платёж, второго не будет |
| Провайдер создал платёж, а COMMIT привязки не прошёл (сбой БД, падение процесса) | попытка остаётся `creating` с сохранённым ключом; повтор (checkout или `reconcile`) получает тот же платёж и привязывает его |
| Ответ на создание потерян, а уведомление пришло | обработчик находит попытку по `metadata.payment_attempt` **из ответа провайдера** и привязывает |
| Попытка без ответа старше 23 часов (ключ ЮKassa живёт 24 ч) | `requires_action` / `unresolved_attempt`; новая попытка **не создаётся** (раздел 5а) |
| Противоречивый ответ на создание (не тот магазин, режим, сумма, metadata; `status` и `paid` не согласованы; ссылка не https или чужой домен) | ссылка покупателю **не выдаётся**, попытка не привязывается, `requires_action` / `create_response_mismatch:<что>`; новые попытки заблокированы до решения человека |
| 4xx при создании (`ProviderRejected`, `ProviderAuthError`) | попытка `failed`, ошибка в журнале, покупателю 503 без подробностей |
| Провайдер недоступен при обработке уведомления | событие `failed`, повтор с растущей паузой (30 с … 1 ч) |
| Уведомление с несуществующим ID | событие `ignored` (`not_found_at_provider`), заказ не меняется |
| Повторная доставка | одна строка `provider_events` (`deliveries` += 1); уже обработанное не обрабатывается снова. Доставка **во время** обработки не трогает аренду, а ставит `recheck_requested`: после завершения событие возвращается в очередь и перепроверяется у провайдера |
| Параллельная обработка | событие у одного обработчика (аренда, раздел 5б); учёт — под блокировкой строки платежа и заказа; второй получает `already_final`; остаток списывается один раз |
| Уведомления не по порядку | всегда проверяется текущее состояние у провайдера; конечный статус не меняется |
| Процесс упал после захвата события | аренда 5 минут; после её истечения событие забирает другой обработчик с новым `lease_token`. Проснувшийся старый обработчик ничего не перезапишет (`stale_lease`) |
| Уведомление не пришло совсем | `reconcile()` раз в 5 минут проверяет незавершённые платежи |
| Деньги пришли, товара не хватает | платёж `succeeded`, деньги в `money_operations`, заказ `requires_action`, остатки не тронуты. Решение — `admin_retry_paid_order(order_id)` после пополнения склада или `request_refund(...)` |
| Оплачен отменённый заказ | деньги учтены, заказ не меняется, запрос возврата `order_not_payable` |

## 5а. «Потерянная» попытка: результат создания неизвестен дольше срока ключа

Ключ идемпотентности ЮKassa действует 24 часа; после этого повтор с тем же ключом может создать **второй** платёж. Поэтому:

1. Пока попытке меньше 23 часов — повтор только с тем же ключом (checkout или сверка).
2. Старше 23 часов — попытка становится `requires_action` / `unresolved_attempt`. Повтор с ключом больше не делается, **новая попытка для заказа не создаётся** (`payment_under_review`, 409). Истечение ключа не считается доказательством отсутствия платежа.
3. Сверка ищет платёж в списке платежей магазина (`GET /v3/payments?created_at.gte=…&created_at.lt=…`, окно «создание попытки −10 минут … +25 часов», все страницы) по `metadata.payment_attempt` и `metadata.order_id`. Повторяет поиск раз в час, 35 дней.
   - Нашёлся — сначала полная проверка ответа провайдера: metadata (заказ и попытка), магазин, режим `test`, согласованность `status`/`paid`, а для неоплаченного — ещё сумма и валюта. Расхождение — `requires_action` с причиной, ссылка не сохраняется.
   - Оплачен — заказ проводится (один раз); отменён — `canceled`, оплата снова доступна.
   - Ждёт оплаты (`pending`) **и** ссылка `https` на разрешённом домене (`YOOKASSA_CONFIRMATION_HOSTS`) — ссылка сохраняется, попытка снова `pending`; checkout отдаёт покупателю **ту же** ссылку, второго платежа нет (rev3).
   - Ссылка может появиться позже: при повторной сверке уже привязанного платежа (`GET /v3/payments/{id}`) проверенная ссылка передаётся в `payment_record_provider_state` и сохраняется **одним UPDATE со сменой статуса на `pending`** под блокировкой строки платежа (rev4). Без https-ссылки база не переведёт «потерянную» попытку в `pending` — это проверяют и функция, и триггер `payments_guard`, даже для владельца таблиц.
   - Ждёт оплаты, но ссылки нет или она непригодна — попытка честно остаётся `requires_action` / `unresolved_attempt` (checkout — 409, статус — `requires_action`, в журнал — `payment.unresolved_checked`). Сверка проверяет платёж каждые 5 минут; когда ЮKassa его отменит, попытка станет `canceled` и оплата откроется. Если он всё же будет оплачен — обычный учёт.
   - Список просмотрен целиком и платежа нет — попытка остаётся `requires_action`. Отсутствие в списке тоже не считается доказательством: решает человек.
   - Список просмотрен не целиком (ошибка, 5000+ платежей в окне) — никакого вывода.
   - Уведомление по такому платежу тоже находит попытку (по metadata из ответа провайдера) и продолжает обработку.
4. Администратор проверил кабинет ЮKassa: `admin_resolve_payment(<id>, 'not_paid_confirmed', 'основание')`. Покупателю показывается `payment_status = canceled`, оплата снова доступна. Если деньги по такой попытке всё же придут позже — они учитываются (`late_success_recorded`), создаётся запрос возврата, решение администратора снимается (причина `late_payment`), событие пишется в журнал.

## 5б. Очередь уведомлений: аренда

| Переход | Условие |
|---|---|
| `received`/`failed` → `processing` | `next_attempt_at` наступил; новый `lease_token` (UUID), `lease_expires_at = now() + 5 мин`, `attempts + 1`; `FOR UPDATE SKIP LOCKED` |
| `processing` → `processing` (другой обработчик) | только если аренда истекла; новый `lease_token` |
| `processing` → `processed` / `ignored` / `failed` | `UPDATE … WHERE status = 'processing' AND lease_token = <свой>`; иначе результат не записывается (`stale_lease`) |
| `processing` → `received` | при завершении, если во время обработки пришла повторная доставка (`recheck_requested`) |
| повторная доставка | `processing`: аренда и срок не меняются, ставится `recheck_requested`; `received`: срок → сейчас; `failed`: пауза повтора сохраняется; `processed`/`ignored`: только счётчик |

Согласованность полей аренды проверяет ограничение `provider_events_lease_check`. Учёт оплаты идемпотентен, поэтому даже параллельная обработка одного платежа устаревшим и новым обработчиком не приводит к двойному списанию; аренда защищает итоговую запись события.

## 5в. Перенаправления от API провайдера

Адаптер ЮKassa не выполняет HTTP-перенаправления (3xx) ни для каких запросов: стандартный обработчик `urllib` повторил бы запрос по адресу из `Location` вместе с `Authorization` (shopId и секретный ключ) и `Idempotence-Key`, в том числе на чужой домен. Ответ 3xx — «результат неизвестен» (`ProviderUnavailable`), без повтора. Для создания платежа это значит: попытка `unknown`, позже повтор к API ЮKassa (не по `Location`) с тем же ключом идемпотентности; новая попытка не открывается.

## 6. Ответ на уведомление

`POST /api/v2/payments/yookassa/webhook` отвечает `200` **только после COMMIT** записи в `provider_events`. Ошибка базы — `500`, и ЮKassa повторит доставку (в течение 24 часов). Неверный формат — `400`, адрес не из списка ЮKassa — `403`, платежи выключены — `404`. Проверка адреса отправителя (`YOOKASSA_WEBHOOK_IP_CHECK`, по умолчанию включена) — дополнительная мера, а не основная: основная — повторный запрос состояния у провайдера.

За обратным прокси (nginx) задайте `TRUSTED_PROXY_IPS`, иначе все запросы будут «от прокси»: заголовок `X-Forwarded-For` учитывается только от этих адресов.

## 7. Настройка (`.env`, пример — `.env.example`)

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `PAYMENTS_ENABLED` | `false` | включить оплату |
| `YOOKASSA_SHOP_ID` | — | shopId магазина (цифры) |
| `YOOKASSA_SECRET_KEY` | — | секретный ключ; `test_...` для тестового магазина |
| `YOOKASSA_MODE` | `test` | `test` или `live` |
| `PAYMENTS_ALLOW_LIVE` | — | для `live` обязательно `I_UNDERSTAND_REAL_MONEY` |
| `PAYMENT_RETURN_URL` | — | куда ЮKassa вернёт покупателя (https; http только localhost в test) |
| `YOOKASSA_TIMEOUT_SECONDS` | `10` | таймаут одного запроса (1..60) |
| `YOOKASSA_MAX_ATTEMPTS` | `3` | попыток на запрос (1..5) |
| `YOOKASSA_WEBHOOK_IP_CHECK` | `true` | проверять адрес отправителя уведомлений |
| `YOOKASSA_CONFIRMATION_HOSTS` | `yoomoney.ru,yookassa.ru` | домены, на которые можно отправить покупателя (ссылка оплаты из ответа ЮKassa; поддомены разрешены) |
| `TRUSTED_PROXY_IPS` | — | адреса обратных прокси через запятую |
| `HTTP_RATE_LIMITS` | `on` | ограничение частоты; `off` — только для тестов |

Неверная конфигурация останавливает запуск приложения с понятной ошибкой (без значения секрета).

В личном кабинете ЮKassa: адрес уведомлений `https://<ваш-домен>/api/v2/payments/yookassa/webhook`, события `payment.succeeded`, `payment.canceled`, `payment.waiting_for_capture`.

## 8. Обработчик и администрирование

```powershell
.\.venv\Scripts\python.exe -m payments.worker --once        # один проход (планировщик задач Windows)
.\.venv\Scripts\python.exe -m payments.worker --interval 10 # постоянно
```

Проход: недосозданные попытки → поиск «потерянных» попыток у провайдера → незавершённые платежи → очередь уведомлений. Несколько обработчиков одновременно — безопасно.

Под ролью администратора (`DB_ROLES.md`):

```sql
-- что требует внимания
SELECT id, order_id, status, status_reason, amount_minor FROM payments WHERE status = 'requires_action';
SELECT * FROM refunds WHERE status = 'requested';
SELECT * FROM provider_events WHERE status = 'failed' OR last_error IS NOT NULL;
-- разрешить спорную попытку после проверки в кабинете ЮKassa (иначе заказ нельзя оплатить снова)
SELECT admin_resolve_payment(<payment_id>, 'not_paid_confirmed', 'проверено в кабинете ЮKassa, платежа нет');
SELECT admin_resolve_payment(<payment_id>, 'handled_manually', 'возврат сделан в кабинете');
-- провести оплаченный заказ после пополнения склада
SELECT admin_retry_paid_order(<order_id>);
-- запросить возврат (исполнить вручную в кабинете ЮKassa)
SELECT request_refund(<payment_id>, <сумма_в_минимальных_единицах_ПОЛУЧЕННОЙ_валюты>, 'причина');
```

## 9. Чего нет (не считать готовым)

- Исполнение возвратов через API ЮKassa и их учёт как `money_operations(kind='refund')`.
- Чеки 54-ФЗ (`receipt`): если магазин в ЮKassa подключён к онлайн-кассе, создание платежа без чека будет отклонено (`ProviderRejected`).
- Двухстадийная оплата (`capture: false`): используется `capture: true`; статус `waiting_for_capture` обрабатывается, но списание не подтверждается — ЮKassa отменит такой платёж сама.
- Проверка на настоящем тестовом магазине ЮKassa (см. `PR3_NOTES.md`): поведение проверено против локального имитатора протокола.
- Оплата заказов API v1 и заказов, созданных до PR 3 (у них нет секрета доступа).
- Ограничение частоты хранится в памяти одного процесса.
