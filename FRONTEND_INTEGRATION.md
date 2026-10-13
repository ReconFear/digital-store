# Подключение frontend к Digital Store — инструкция для Артёма

Здесь описано, как подключить любую HTML/CSS-страницу к backend Digital Store через готовый JavaScript-клиент. React, npm и сборщики не нужны: клиент — один файл, обычный ES-модуль.

Точный формат ответов API описан в [`FRONTEND_API.md`](FRONTEND_API.md).

## Что лежит в папке `frontend/`

```text
frontend/
├── lib/
│   └── digital-store-api.js        ← API-клиент. Его вы подключаете к своему дизайну
├── examples/
│   └── vanilla-storefront/         ← демо-витрина: пример использования клиента
│       ├── index.html, app.js, styles.css
│       ├── config.js               ← адрес backend для демо
│       └── order-test.html/.js     ← ТЕСТОВАЯ страница создания заказа (только для разработки)
├── tests/                          ← тесты клиента: открываются в браузере
└── serve.py                        ← локальный HTTP-сервер для этой папки
```

Клиент не зависит от демо: не использует его CSS, классы и разметку. Демо можно не копировать.

## 1. Запустить backend

В корне проекта, PowerShell:

```powershell
.\.venv\Scripts\python.exe -m uvicorn api:app --reload
```

Проверка: в браузере открывается http://127.0.0.1:8000/products и показывает JSON.

> По умолчанию backend берёт базу из `.env` — обычно это основная `digital_store`. Каталог только читается, поэтому это безопасно. Для тестовой страницы заказа запускайте backend на тестовой базе (см. раздел 7).

## 2. Запустить frontend через HTTP-сервер (не через `file://`)

Если открыть HTML двойным щелчком (адрес начинается с `file://`), ES-модули не загрузятся, а backend получит `Origin: null` и откажет по CORS. Страницы нужно открывать через локальный HTTP-сервер.

**Вариант А — встроенный сервер проекта (рекомендуется).** Во втором окне PowerShell, в корне проекта:

```powershell
.\.venv\Scripts\python.exe frontend\serve.py
```

Открыть:

- демо-витрина: http://127.0.0.1:5500/examples/vanilla-storefront/
- тесты клиента: http://127.0.0.1:5500/tests/

`serve.py` раздаёт только папку `frontend/` и только на этом компьютере (127.0.0.1). Не запускайте `python -m http.server` из корня проекта: такой сервер по умолчанию доступен из локальной сети и отдаст любой файл проекта, включая `.env` с паролем от базы.

**Вариант Б — VS Code Live Server.** Тоже подходит: по умолчанию он работает на порту 5500, а этот порт уже разрешён в CORS.

## 3. Какие адреса страниц (origins) разрешены

Браузер пускает страницу к API только если её origin (схема + хост + порт) разрешён на backend. По умолчанию для разработки разрешены:

```text
http://localhost:5500    http://127.0.0.1:5500
http://localhost:5173    http://127.0.0.1:5173
```

`localhost` и `127.0.0.1` для браузера — **разные** origins. Если вашего адреса нет в списке, добавьте его в `.env` backend и перезапустите uvicorn:

```dotenv
CORS_ALLOWED_ORIGINS=http://localhost:5500,http://127.0.0.1:5500,http://localhost:3000
```

Если переменная задана, она **заменяет** список по умолчанию, поэтому перечисляйте все нужные адреса. Значения `*` и `null` и адреса с путём запрещены: backend откажется запускаться и объяснит причину.

CORS — это не защита данных. Он лишь не даёт **чужим сайтам в браузере** читать ответы API. Запросы из curl, Postman или с сервера CORS не ограничивает.

## 4. Подключить клиент к своей странице

Скопируйте `frontend/lib/digital-store-api.js` в свой проект (или подключите по относительному пути) и используйте его в скрипте с `type="module"`:

```html
<ul id="products"></ul>
<p id="error" hidden></p>

<script type="module">
  import { createApiClient, isInStock } from "./digital-store-api.js";

  // Единственное место, где указан адрес backend.
  const api = createApiClient({ baseUrl: "http://127.0.0.1:8000" });

  const list = document.getElementById("products");
  const errorBox = document.getElementById("error");

  try {
    const { products } = await api.getProducts();
    for (const product of products) {
      const item = document.createElement("li");
      // textContent, а не innerHTML: так название товара не сможет выполнить чужой код.
      item.textContent = `${product.name} — ${product.price} ₽` + (isInStock(product) ? "" : " (нет в наличии)");
      list.append(item);
    }
  } catch (error) {
    errorBox.textContent = error.userMessage;   // готовый текст для покупателя
    errorBox.hidden = false;
    console.error(error);                       // подробности для разработчика
  }
</script>
```

Если ваша страница использует строгую политику CSP, вынесите этот код в отдельный `.js`-файл (как в демо), а не пишите его внутри `<script>`.

### Методы клиента

| Вызов | Запрос | Результат |
|---|---|---|
| `api.getProducts()` | `GET /products` | `{ products, total }` — товары в продаже, включая закончившиеся |
| `api.getProducts({ availableOnly: true })` | `GET /products?available_only=true` | только товары с остатком > 0 |
| `api.getProduct(id)` | `GET /products/{id}` | один товар; если его нет или он снят с продажи — ошибка `not_found` |
| `api.createOrder(productId)` | `POST /orders` | **выключен по умолчанию**, см. раздел 7 |
| `isInStock(product)` | — | `true`, если товар в продаже и `quantity > 0` |

Поля товара — ровно те, что отдаёт backend:

```json
{ "id": 1, "name": "Minecraft Key", "price": 999, "quantity": 14, "is_for_sale": true }
```

`price` — целое число в **рублях**. Других полей (картинок, описаний, категорий) backend пока не отдаёт, их нельзя ожидать.

У каждого метода есть необязательный параметр `signal` для отмены запроса (`AbortController`). Таймаут по умолчанию — 10 секунд: `createApiClient({ baseUrl, timeoutMs: 5000 })`.

### Ошибки

Все ошибки клиента — это `DigitalStoreApiError` с полями:

| Поле | Значение |
|---|---|
| `code` | что случилось (таблица ниже) |
| `userMessage` | готовый текст на русском, можно показывать покупателю |
| `status` | HTTP-статус или `null`, если ответа не было |
| `detail` | поле `detail` из ответа backend |
| `retryable` | `true`, если имеет смысл кнопка «Повторить» |
| `message` | техническое описание для консоли |

| `code` | Когда |
|---|---|
| `network` | backend выключен, неверный адрес **или запрос заблокирован CORS** — браузер не различает эти случаи |
| `timeout` | backend не ответил за `timeoutMs` |
| `not_found` | 404: товара нет или он снят с продажи |
| `unavailable` | 409: товар нельзя заказать (снят с продажи или закончился) |
| `validation` | 422: неверные данные запроса |
| `forbidden` | 403 |
| `server` | 5xx: ошибка на сервере |
| `invalid_response` | ответ не JSON или неожиданного формата |
| `invalid_argument` | неверный `id` передан в метод — запрос даже не отправлялся |
| `orders_disabled` | вызван `createOrder` без явного разрешения |
| `aborted` | запрос отменён через `signal` |

Тексты `userMessage` можно заменить своими, ориентируясь на `code`.

## 5. Как поменять адрес backend

- В своём проекте — параметр `baseUrl` в `createApiClient`.
- В демо — файл `frontend/examples/vanilla-storefront/config.js`.

`baseUrl` может содержать путь, если API стоит за прокси: `https://shop.example.com/api` → запросы пойдут на `https://shop.example.com/api/products`.

## 6. Проверить связь frontend ↔ backend

1. Backend запущен: http://127.0.0.1:8000/products открывается.
2. Frontend запущен через `serve.py` или Live Server.
3. Открыть http://127.0.0.1:5500/tests/ — должно быть «30 из 30 тестов пройдено». Эти тесты к серверу не обращаются: они проверяют, что клиент правильно загружается и работает в вашем браузере.
4. Открыть http://127.0.0.1:5500/examples/vanilla-storefront/ — должны появиться карточки товаров из базы.
5. Остановить uvicorn (Ctrl+C) и обновить страницу — должно появиться сообщение об ошибке и кнопка «Повторить». Запустить uvicorn, нажать «Повторить» — каталог вернётся.

## 7. Заказы и оплата: что есть и чего нет

`POST /orders` создаёт **настоящую запись заказа** в базе в статусе `pending`. И всё:

- оплаты нет, и подтвердить оплату с frontend нельзя — так задумано;
- товар не резервируется и не выдаётся;
- в заказе нет данных покупателя, посмотреть свой заказ пока нельзя.

Поэтому кнопку «Купить» к `createOrder` подключать **нельзя**. В клиенте метод выключен по умолчанию. Включить его можно только явно, для тестов:

```js
const api = createApiClient({ baseUrl: "http://127.0.0.1:8000", allowOrderCreation: true });
```

Для проверки связи есть отдельная страница http://127.0.0.1:5500/examples/vanilla-storefront/order-test.html. Витрина на неё не ссылается, и перед отправкой нужно поставить галочку подтверждения. Перед её использованием запустите backend на **тестовой** базе:

```powershell
$env:DB_NAME = "digital_store_test"
.\.venv\Scripts\python.exe -m uvicorn api:app --reload
# после проверки: Ctrl+C и
Remove-Item Env:DB_NAME
```

Оформление покупки появится вместе с платёжным провайдером. Контракт будет согласован отдельно, до этого — только mock-данные.

## 8. PR 2: что изменилось для frontend (коротко)

**Ничего переписывать не нужно.** Витрина на `getProducts()` / `getProduct()` работает как раньше: старые адреса и поля не менялись. Единственное отличие: товары, которые старый формат не может описать (цена с копейками, другая валюта, товары без учёта остатка — услуги и файлы), в старом API не появляются. Сейчас таких товаров в базе нет.

Новое — **API v2** для корзины и оформления заказа из нескольких товаров. Методы в том же клиенте:

| Метод | Что делает |
|---|---|
| `api.getProductsV2({ availableOnly })` | каталог v2: цены в копейках, `product_type`, `stock_quantity` (`null` — остаток не учитывается), `available` |
| `api.getProductV2(id)` | один товар v2 |
| `api.createOrderV2(items)` | заказ из нескольких позиций (выключен без `allowOrderCreation: true`) |
| `formatMoney(price)` | `{ amount_minor: 99950, currency: "RUB", exponent: 2 }` → «999,50 ₽» |

Формат и ошибки — в `FRONTEND_API.md`, раздел «API v2».

### Корзина: что подготовить

1. **Корзина живёт в браузере.** Сервер её не хранит. Храните в `localStorage` только `product_id` и `quantity`:

   ```js
   // корзина: [{ product_id: 1, quantity: 2 }, { product_id: 7, quantity: 1 }]
   ```

   Цены и названия в корзине можно показывать из каталога, но **не отправляйте их на сервер** и не считайте окончательными.

2. **Деньги — целыми копейками.** Предварительный итог в корзине: `сумма(price.amount_minor × quantity)` — это целые числа, точность не теряется. Показывать — через `formatMoney()`. Не делите на 100 для расчётов.

3. **Наличие.** Кнопку «В корзину» блокируйте по `available`. Ограничение количества: для `inventory_mode: "tracked"` — не больше `stock_quantity` и не больше 1000; для `unlimited` — не больше 1000.

4. **Оформление.** `createOrderV2(cart)`. Итог, цены и названия на странице «Заказ создан» берите **из ответа сервера** (`order.total`, `order.items`): они могут отличаться от корзины, если цена изменилась. Сохраните `order.id` и `accessToken` — по ним заказ можно прочитать снова (`getOrderStatusV2`, PR 3).

5. **Ошибки оформления** — по `error.code`:

   | `error.code` | Что показать / сделать |
   |---|---|
   | `insufficient_stock` | «Недостаточно товара»; подсветить позиции из `error.detail.product_ids`, обновить каталог |
   | `not_found` | товар больше не продаётся — убрать из корзины позиции `error.detail.product_ids` |
   | `mixed_currency`, `unsupported_currency` | «Эти товары нельзя оформить одним заказом» |
   | `validation` | ошибка в данных корзины (например, количество 0) — проверить код корзины |
   | `network`, `timeout`, `server` | «Повторить» |

6. **Тексты.** Заказ создаётся со статусом `pending`: оплаты пока нет. На странице результата — «Заказ № … создан», без «Оплачено» и «Покупка завершена». Кнопку оформления в публичной витрине пока не включайте: сначала нужен платёжный провайдер.

### Пример оформления

```js
import { createApiClient, formatMoney } from "./digital-store-api.js";

const api = createApiClient({ baseUrl: "http://127.0.0.1:8000", allowOrderCreation: true }); // только тестовая база!

try {
  const { order } = await api.createOrderV2(cart);
  showMessage(`Заказ № ${order.id} создан. Сумма: ${formatMoney(order.total)}. Оплата появится позже.`);
  localStorage.removeItem("cart");
} catch (error) {
  if (error.code === "insufficient_stock") highlight(error.detail.product_ids);
  showMessage(error.userMessage);
}
```

## 8а. PR 3: оплата (для Артёма)

Старые методы не менялись. `createOrderV2` теперь возвращает ещё и `accessToken` — секрет владельца заказа:

```js
const { order, accessToken } = await api.createOrderV2(cart);
sessionStorage.setItem("lastOrder", JSON.stringify({ id: order.id, accessToken }));  // не в URL!
```

Новые методы клиента:

| Метод | Что делает |
|---|---|
| `api.checkoutV2(orderId, accessToken)` | начать оплату → `{ payment_status: "awaiting_payment", confirmation_url }` (выключен без `allowOrderCreation: true`) |
| `api.getOrderStatusV2(orderId, accessToken)` | статус: `order.status`, `payment_status`, `fulfillment_status`, `refund_status` |

Сценарий:

1. «Оформить» → `createOrderV2(cart)` → сохранить `{ id, accessToken }` в `sessionStorage`.
2. «Оплатить» → `checkoutV2(id, accessToken)` → `location.assign(confirmation_url)`. Повторное нажатие безопасно: вернётся та же ссылка.
3. Страница возврата (`PAYMENT_RETURN_URL`) → взять `{ id, accessToken }` из `sessionStorage` → опрашивать `getOrderStatusV2` раз в 3–5 секунд, пока `payment_status` = `awaiting_payment` (не дольше 2–3 минут).
4. Тексты: `succeeded` + `order.status = "paid"` → «Оплачено»; `order.status = "requires_action"` → «Оплата получена, магазин свяжется с вами»; `canceled` → «Оплата не прошла» + кнопка «Оплатить снова»; `awaiting_payment` после ожидания → «Ждём подтверждения банка, обновите страницу позже».

Никогда не показывайте «Оплачено» по самому факту возврата с ЮKassa или по ответу `checkoutV2` — только по `getOrderStatusV2`.

Ошибки (`error.code`): `order_not_found` (нет заказа или неверный секрет), `order_not_payable`, `payment_under_review` («Оплата проверяется магазином», кнопку «Оплатить» скрыть), `payments_disabled`, `payment_provider_unavailable` («Повторить через минуту»), `rate_limited`. `confirmation_url = null` в ответе `checkoutV2` — не перенаправлять, показать статус.

Оплата по умолчанию выключена на сервере (`payments_disabled`). Для локальной проверки нужен тестовый магазин ЮKassa (`PAYMENTS.md`, раздел 7) — без него показывайте сообщение об ошибке.

## 9. Частые проблемы

| Что видно | Причина | Что делать |
|---|---|---|
| В консоли: `blocked by CORS policy: No 'Access-Control-Allow-Origin' header` | origin страницы не разрешён | Сравните адрес страницы в браузере (хост **и порт**) со списком из раздела 3. Добавьте его в `CORS_ALLOWED_ORIGINS` и перезапустите uvicorn |
| То же, а страница открыта как `file:///...` | нет HTTP-сервера | Запустите `frontend\serve.py` или Live Server |
| `Failed to load module script ... MIME type "text/plain"` | сервер отдаёт `.js` с неверным типом (бывает на Windows) | Используйте `frontend\serve.py` |
| `ERR_CONNECTION_REFUSED`, на странице «Не удалось связаться с сервером» | backend не запущен или другой порт | Запустите uvicorn; проверьте `baseUrl` (по умолчанию `http://127.0.0.1:8000`) |
| 404 `{"detail": "Not Found"}` | опечатка в пути или в `baseUrl` (например, лишний `/api`) | Проверьте `baseUrl`. Не путать с `"Product not found"` — это «товара нет» |
| Каталог пустой | в базе нет товаров в продаже | Это не ошибка; для разработки загрузите `seed.sql` в тестовую базу |
| Изменения в JS не видны | кеш браузера | Ctrl+F5; `serve.py` кеш отключает |
| `localhost` работает, а `127.0.0.1` нет (или наоборот) | это разные origins | Разрешите оба или открывайте страницу по разрешённому адресу |
