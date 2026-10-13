# Digital Store — API для фронтенда

Актуально для PR 2 «Universal Commerce Core». Как подключить готовый JS-клиент — в [`FRONTEND_INTEGRATION.md`](FRONTEND_INTEGRATION.md).

В API две версии:

| | v1 — старый контракт | v2 — универсальное ядро |
|---|---|---|
| Адреса | `/products`, `/products/{id}`, `/orders` | `/api/v2/products`, `/api/v2/products/{id}`, `/api/v2/orders` |
| Цена | `price`: целые рубли | `price`: `{ amount_minor, currency, exponent }`, копейки |
| Остаток | `quantity` | `stock_quantity` (`null`, если не учитывается) + `available` |
| Заказ | один товар | несколько позиций |
| Статус | **не менялся**, действующая витрина работает как раньше | новый, для корзины и оформления |

Разделы 1–5 ниже — v1 (без изменений). API v2 — в разделе «API v2».

## Адрес backend

При локальной разработке:

`http://127.0.0.1:8000`

Интерактивная документация (Swagger): `http://127.0.0.1:8000/docs`

### CORS

По умолчанию (переменная `CORS_ALLOWED_ORIGINS` не задана) браузерный доступ разрешён страницам с адресов:
`http://localhost:5500`, `http://127.0.0.1:5500`, `http://localhost:5173`, `http://127.0.0.1:5173`.

- Список меняется переменной окружения backend `CORS_ALLOWED_ORIGINS` (через запятую). Заданная переменная **заменяет** список по умолчанию.
- Разрешённые методы: `GET`, `POST`. Разрешённый дополнительный заголовок: `Content-Type`. Cookies и авторизационные данные не передаются (`allow_credentials = false`).
- Preflight `OPTIONS` от неразрешённого origin, с другим методом (`PUT`, `DELETE`…) или с другими заголовками (например, `Authorization`) получает `400`.
- Ответ на запрос с неразрешённого origin формируется, но без заголовка `Access-Control-Allow-Origin`, поэтому браузер не отдаёт его странице. Прямые запросы (curl, Postman) CORS не ограничивает: это не механизм защиты.

## Важное про цены и ID

- `price` — целое число в рублях (например, `999` = 999 ₽). Планируется переход на копейки + валюту (PR 2); об этом будет отдельное предупреждение и переходный период.
- ID товаров и заказов — целые числа. Не стоит полагаться на то, что ID идут подряд.

## 1. Каталог товаров

**GET** `/products`

Возвращает товары, которые **находятся в продаже** (`is_for_sale = true`), в том числе закончившиеся (`quantity = 0`) — их можно показывать как «Нет в наличии».

Товары, снятые с продажи (`is_for_sale = false`), **не возвращаются**.

Ответ `200 OK`:

```json
{
  "products": [
    {
      "id": 1,
      "name": "Minecraft Key",
      "price": 999,
      "quantity": 14,
      "is_for_sale": true
    }
  ],
  "total": 1
}
```

Это пример формата. Настоящий список и остатки могут отличаться.

> Изменение в PR 1: раньше без параметров возвращались все товары, включая снятые с продажи. Формат ответа не изменился.

## 2. Только товары, которые можно купить сейчас

**GET** `/products?available_only=true`

Товары в продаже и с остатком больше нуля. Формат ответа тот же.

## 3. Товар по ID

**GET** `/products/{product_id}`

Ответ `200 OK` — объект товара в том же формате, что элемент списка `products`.

`404` — товара нет **или он снят с продажи**:

```json
{
  "detail": "Product not found"
}
```

## 4. Создать заказ

**POST** `/orders`

Тело запроса:

```json
{
  "product_id": 1
}
```

`product_id` — целое число больше нуля.

Успешный ответ `200 OK`:

```json
{
  "message": "Order created",
  "order": {
    "id": 1,
    "product_id": 1,
    "price": 999,
    "status": "pending"
  }
}
```

- `status: "pending"` — заказ создан и ждёт оплаты. Товар при этом не резервируется и остаток не уменьшается.
- Оплата пока не подключена: перевести заказ в оплаченный через API нельзя, и это намеренно.

Ошибки:

| Код | `detail` | Когда |
|---|---|---|
| `404` | `Product not found` | Товара с таким ID нет |
| `409` | `Product unavailable` | Товар снят с продажи или закончился |
| `422` | (ошибка валидации FastAPI) | Неверное тело запроса, например `product_id <= 0` |

## 5. Список заказов

**GET** `/orders`

Публичный просмотр заказов запрещён.

Ответ `403 Forbidden`:

```json
{
  "detail": "Order listing is not public"
}
```

Доступ покупателя к своим заказам появится вместе с аутентификацией.

## Возможные статусы заказа

| Статус | Значение |
|---|---|
| `pending` | Создан, ждёт оплаты |
| `paid` | Оплата подтверждена backend-ом (появится вместе с платёжным провайдером) |
| `cancelled` | Отменён |
| `legacy` | Старый заказ, созданный до появления статусов |

## Общие ответы FastAPI

Кроме описанных выше, FastAPI отдаёт стандартные ответы:

| Ситуация | Код | Тело |
|---|---|---|
| Неизвестный путь (опечатка в URL) | `404` | `{"detail": "Not Found"}` — не путать с `"Product not found"` |
| Неподдерживаемый метод для существующего пути | `405` | `{"detail": "Method Not Allowed"}` |
| Неверные данные: тело `POST /orders`, нечисловой `id` в `/products/{id}` | `422` | `{"detail": [ … ]}` — **массив** объектов ошибок |

Элемент массива `422` содержит как минимум `type` (например, `"greater_than"` для `product_id <= 0` — это закреплено тестом), `loc` (где ошибка, например `["body", "product_id"]`) и `msg` (текст на английском). Показывать `msg` покупателю не рекомендуется.

Итого: `detail` — строка для ошибок `403/404/405/409` и массив для `422`.

## API v2

### Деньги в v2

```json
{ "amount_minor": 99950, "currency": "RUB", "exponent": 2 }
```

Сумма = `amount_minor / 10^exponent` = 999,50 ₽. `amount_minor` — всегда целое. Не делите его во float для расчётов. Для показа есть `formatMoney()` в JS-клиенте.

### Каталог v2

**GET** `/api/v2/products` (и `?available_only=true`)

Товары в продаже, включая те, у которых остаток не учитывается (услуги, файлы). Снятые с продажи не возвращаются.

```json
{
  "products": [
    {
      "id": 1,
      "name": "Minecraft Key",
      "product_type": "other",
      "inventory_mode": "tracked",
      "price": { "amount_minor": 99900, "currency": "RUB", "exponent": 2 },
      "stock_quantity": 14,
      "available": true
    },
    {
      "id": 7,
      "name": "Консультация",
      "product_type": "service",
      "inventory_mode": "unlimited",
      "price": { "amount_minor": 300050, "currency": "RUB", "exponent": 2 },
      "stock_quantity": null,
      "available": true
    }
  ],
  "total": 2
}
```

- `product_type`: `digital_key`, `digital_file`, `physical`, `service`, `other` (у всех старых товаров — `other`).
- `inventory_mode`: `tracked` (есть остаток) или `unlimited`.
- `stock_quantity`: остаток или `null` для `unlimited`.
- `available`: можно ли заказать сейчас (`unlimited` — всегда `true`; `tracked` — если остаток > 0).
- `available_only=true` оставляет только `available: true`.

**GET** `/api/v2/products/{id}` — один товар в том же формате. Нет или снят с продажи → `404`:

```json
{ "detail": { "code": "product_not_found", "message": "Товар не найден или не продаётся.", "product_ids": [5] } }
```

### Создать заказ v2

**POST** `/api/v2/orders`

```json
{
  "items": [
    { "product_id": 1, "quantity": 2 },
    { "product_id": 7, "quantity": 1 }
  ]
}
```

- От 1 до 50 позиций. `product_id` и `quantity` — целые числа (строки `"2"` не принимаются), `quantity` от 1 до 1000.
- Цену, итог, валюту и статус присылать **нельзя**: любые лишние поля → `422`. Всё считает сервер.
- Повторяющиеся `product_id` объединяются, количества складываются.

Успех — `201 Created`:

```json
{
  "order": {
    "id": 42,
    "status": "pending",
    "created_at": "2026-10-13T12:00:00.000000+03:00",
    "total": { "amount_minor": 499850, "currency": "RUB", "exponent": 2 },
    "items": [
      {
        "product_id": 1,
        "name": "Minecraft Key",
        "product_type": "other",
        "quantity": 2,
        "unit_price": { "amount_minor": 99900, "currency": "RUB", "exponent": 2 },
        "line_total": { "amount_minor": 199800, "currency": "RUB", "exponent": 2 }
      },
      {
        "product_id": 7,
        "name": "Консультация",
        "product_type": "service",
        "quantity": 1,
        "unit_price": { "amount_minor": 300050, "currency": "RUB", "exponent": 2 },
        "line_total": { "amount_minor": 300050, "currency": "RUB", "exponent": 2 }
      }
    ]
  },
  "access_token": "q8Jx2...43 символа...Zk_w"
}
```

`status: "pending"` — заказ создан и ждёт оплаты. Товар не резервируется. Покажите покупателю номер заказа и итог от сервера, но не «Покупка завершена».

`access_token` (PR 3) — секрет владельца заказа, 43 символа. Сервер показывает его **один раз** и хранит только хэш. Без него нельзя оплатить заказ и узнать его статус. Храните его на стороне покупателя (например, в `sessionStorage` вместе с номером заказа), не передавайте в URL, не пишите в логи и аналитику.

Ошибки бизнес-правил — `detail` объект с `code`:

| Код HTTP | `detail.code` | Когда |
|---|---|---|
| `404` | `product_not_found` | товара нет или он снят с продажи |
| `409` | `insufficient_stock` | не хватает остатка (`product_ids` — каких товаров) |
| `409` | `mixed_currency` | товары в разных валютах |
| `409` | `unsupported_currency` | валюта товара не поддерживается магазином |
| `422` | `quantity_too_large` | после объединения повторов больше 1000 единиц одного товара |
| `422` | `too_many_items` | больше 50 разных товаров |
| `422` | `amount_out_of_range` | итог вне допустимого диапазона |

Ошибки **формата** запроса — стандартный `422` FastAPI, `detail` — массив (см. «Общие ответы FastAPI»). Отличить просто: в v2 `detail` — объект для бизнес-ошибок и массив для ошибок формата.

Пример `409`:

```json
{ "detail": { "code": "insufficient_stock", "message": "Недостаточно товара в наличии.", "product_ids": [1] } }
```

### Оплата заказа (PR 3)

**POST** `/api/v2/orders/{order_id}/checkout`

```json
{ "access_token": "<секрет из ответа на создание заказа>" }
```

Ответ `200`:

```json
{ "payment_status": "awaiting_payment", "confirmation_url": "https://yoomoney.ru/checkout/payments/v2/contract?orderId=..." }
```

Перенаправьте покупателя на `confirmation_url` (только если адрес начинается с `https://`). `confirmation_url` может быть `null` — платёж уже не ждёт покупателя; смотрите статус. После оплаты ЮKassa вернёт его на страницу магазина (`PAYMENT_RETURN_URL`). **Этот ответ — не оплата.** Оплаченным заказ становится только после того, как сервер сам проверит платёж у ЮKassa; узнать результат — запросом статуса. Повторный вызов для того же заказа возвращает ту же ссылку (второй платёж не создаётся).

| Код HTTP | `detail.code` | Когда |
|---|---|---|
| `404` | `order_not_found` | нет такого заказа **или** неверный `access_token` (не различаются) |
| `409` | `order_not_payable` | заказ уже оплачен, отменён, ждёт решения магазина, или его сумма 0 |
| `409` | `payment_under_review` | предыдущая попытка оплаты проверяется магазином (результат неизвестен); новая ссылка не выдаётся, чтобы не списать деньги дважды |
| `503` | `payments_disabled` | оплата в магазине выключена |
| `503` | `payment_provider_unavailable` | ЮKassa не ответила — повторите через минуту (безопасно) |
| `429` | `rate_limited` | слишком много запросов (заголовок `Retry-After`, секунды) |

### Статус заказа (PR 3)

**POST** `/api/v2/orders/{order_id}/status` с тем же телом `{ "access_token": "..." }`. POST, а не GET — чтобы секрет не попадал в URL и журналы серверов.

```json
{
  "order": { "id": 42, "status": "paid", "created_at": "...", "total": { ... }, "items": [ ... ] },
  "payment_status": "succeeded",
  "confirmation_url": null,
  "fulfillment_status": "pending",
  "refund_status": "none"
}
```

| Поле | Значения |
|---|---|
| `order.status` | `pending`, `paid`, `requires_action` (оплачено, но нужен магазин — например, товар закончился), `cancelled`, `legacy` |
| `payment_status` | `none` (оплату не начинали), `awaiting_payment`, `succeeded`, `canceled` (можно начать новую оплату), `failed`, `requires_action` (магазин разберётся) |
| `confirmation_url` | ссылка на оплату, пока платёж ждёт покупателя; иначе `null` |
| `fulfillment_status` | `not_started`, `pending`, `in_progress`, `fulfilled`, `requires_action`, `cancelled` |
| `refund_status` | `none`, `pending` (возврат оформляется), `succeeded`, `canceled` |

Страница «возврата с оплаты» опрашивает статус раз в 3–5 секунд (не чаще: лимит 60 запросов в минуту) до `payment_status` ≠ `awaiting_payment` или 2–3 минут ожидания.

### Ограничения запросов (PR 3)

Создание заказа и `checkout` — не больше 10 в минуту с одного адреса, статус — 60 в минуту; тело запроса — до 16 КиБ. Превышение: `429` (`rate_limited`, заголовок `Retry-After`) или `413` (`payload_too_large`).

### Чего в v2 нет

- `GET /api/v2/orders/{id}` и списка заказов: просмотр — только через `/status` с секретом.
- Отмены заказа покупателем, исполнения возврата через API, выдачи товара.

## Что ещё не реализовано

Резервирование, выдача товара, отмена покупателем, исполнение возвратов через провайдера, аутентификация и кабинет покупателя, админ-API. Заказы API v1 оплатить нельзя (у них нет секрета доступа). Корзина хранится на стороне frontend.
