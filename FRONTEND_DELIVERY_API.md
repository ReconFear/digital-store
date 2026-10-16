# Получение цифровых ключей покупателем — контракт API (для Артёма)

Backend PR5 добавляет один маршрут. Файлы `frontend/` в PR5 **не менялись**: метод в JS-клиент добавляешь ты (пример ниже).

## Запрос

```
POST /api/v2/orders/{order_id}/delivery
Content-Type: application/json

{ "access_token": "<секрет из ответа POST /api/v2/orders>" }
```

- `order_id` — целое число из ответа на создание заказа.
- `access_token` — тот же секрет, что для `/status` и `/checkout`, **только в теле**: не в URL, не в query, не в заголовках. Лишние поля тела → `422`.
- Повторять запрос можно сколько угодно: ключи те же, порядок стабилен. Каждый показ ключей записывается в журнал магазина.

## Ответ 200

```json
{
  "order_id": 42,
  "order_status": "paid",
  "items": [
    {
      "order_item_id": 101,
      "product_id": 7,
      "name": "Ключ игры",
      "fulfillment_type": "digital_keys",
      "quantity": 2,
      "delivery_status": "delivered",
      "digital_keys": ["AAAAA-BBBBB-CCCCC", "DDDDD-EEEEE-FFFFF"]
    },
    {
      "order_item_id": 102,
      "product_id": 9,
      "name": "Консультация",
      "fulfillment_type": "manual",
      "quantity": 1,
      "delivery_status": "manual"
    }
  ]
}
```

| Поле | Тип | Описание |
|---|---|---|
| `order_id` | integer | номер заказа |
| `order_status` | string | `pending`, `paid`, `requires_action`, `cancelled`, `legacy` |
| `items[]` | array | позиции заказа в порядке создания |
| `items[].order_item_id` | integer | номер позиции |
| `items[].product_id` | integer | товар |
| `items[].name` | string | название на момент заказа |
| `items[].fulfillment_type` | string | `digital_keys`, `manual`, `downloads`, `physical_shipping` |
| `items[].quantity` | integer | количество |
| `items[].delivery_status` | string | см. таблицу ниже |
| `items[].digital_keys` | string[] | **только** при `delivery_status = "delivered"`; длина = `quantity` |

Поля, которых нет в таблице, не появятся. Значения ключей — произвольные строки до 512 символов (Unicode, с учётом регистра; бывают пробелы и табуляции внутри).

### `delivery_status`

| Значение | Когда | Что показать |
|---|---|---|
| `awaiting_payment` | заказ ещё не оплачен (`order_status = pending`) | «Ждём подтверждения оплаты» |
| `processing` | оплачено, ключи готовятся | «Готовим ключи…» и повторить запрос (правила ниже) |
| `delivered` | ключи выданы, есть `digital_keys` | ключи |
| `delayed` | выдача задерживается (магазину нужно вмешаться) — и для позиции, и когда заказ в `requires_action` | «Выдача задерживается, магазин свяжется с вами». Причину не показывать: её нет в ответе |
| `cancelled` | выдача позиции отменена магазином | «Позиция отменена» |
| `manual` | позиция исполняется вручную (не ключи) | «Магазин выполнит заказ вручную» |
| `not_applicable` | заказ отменён, заказ старого формата или задачи выдачи нет | показать статус заказа |

Решение «оплачено» и «выдано» frontend **не принимает**: только по `order_status` и `delivery_status` из ответа сервера.

## Ошибки

Формат как у остальных маршрутов v2: `{"detail": {"code": "...", "message": "..."}}`.

| HTTP | `detail.code` | Когда | Что делать |
|---|---|---|---|
| 404 | `order_not_found` | нет заказа **или** неверный/пустой секрет (не различаются) | «Заказ не найден» |
| 422 | — (формат FastAPI) | неверное тело (нет `access_token`, лишние поля, не JSON) | ошибка в коде клиента |
| 429 | `rate_limited` | больше 20 запросов в минуту с адреса; заголовок `Retry-After` (секунды) | ждать `Retry-After` |
| 503 | `delivery_unavailable` | временно недоступно (настройка сервера, проверка ключа) | один повтор через 10 секунд, затем «Попробуйте позже» |

## Заголовки ответа

У **всех** ответов этого маршрута (200, 404, 422, 429, 503): `Cache-Control: no-store`, `Pragma: no-cache`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`.

## Правила повтора

- `processing` — опрашивать каждые 3–5 секунд, не дольше 2 минут. Затем показать «Обновите страницу позже».
- `awaiting_payment` — по возвращении со страницы оплаты опрашивать `/status` (как в PR3), а после `paid` — этот маршрут.
- `delayed`, `cancelled`, `manual` — не опрашивать в цикле.
- 503 — один повтор через 10 секунд.
- 429 — ждать `Retry-After`.

## Безопасный показ ключей

- По умолчанию скрыты (маска), кнопки «Показать» и «Копировать».
- Не сохранять в `localStorage`/`sessionStorage`/IndexedDB, не отправлять в аналитику и логи, не писать в `console`.
- Не подставлять в URL, заголовок страницы, `alt`/`title`.
- Выводить через `textContent`, не `innerHTML`.
- При уходе со страницы — убрать из DOM и из переменных.
- `access_token` хранится так же, как для `/status` (PR3).

## Пример для JS-клиента

```js
// frontend/lib/digital-store-api.js — предлагаемый метод (добавляешь сам)
async getDeliveryV2(orderId, accessToken) {
  return this._post(`/api/v2/orders/${encodeURIComponent(orderId)}/delivery`, { access_token: accessToken });
}
```

```js
// показ (фрагмент)
const data = await api.getDeliveryV2(orderId, token);
for (const item of data.items) {
  if (item.delivery_status === "delivered") {
    for (const key of item.digital_keys) {
      const code = document.createElement("code");
      code.textContent = "•".repeat(12);            // маска
      showButton.onclick = () => { code.textContent = key; };
      copyButton.onclick = () => navigator.clipboard.writeText(key);
    }
  }
}
```

Названия `_post` и обработки ошибок — по образцу существующих методов клиента (`getOrderStatusV2`, `checkoutV2`).
