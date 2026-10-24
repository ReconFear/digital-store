# Аккаунты покупателей — API для frontend (PR6)

Контракт раздела `/api/account/*` и оформления заказа с аккаунтом. Дополняет [FRONTEND_API.md](FRONTEND_API.md) (каталог, гостевые заказы, оплата) и [FRONTEND_DELIVERY_API.md](FRONTEND_DELIVERY_API.md) (выдача ключей).

## 0. Состояние функции — прочитать первым

- **Аккаунты выключены по умолчанию.** Включаются только двумя флагами одновременно: в настройках backend (`DS_ACCOUNTS_ENABLED`) и в базе. Пока выключены — маршрутов `/api/account/*` нет (ответ `404`), cookie аккаунта сервер не читает, магазин работает как раньше.
- **Регистрация закрыта по умолчанию** — отдельным флагом в базе: `POST /api/account/register` отвечает `403 registration_closed`, вход для уже созданных аккаунтов при этом работает.
- **Публично включать аккаунты пока нельзя:** нет подтверждения email и восстановления пароля. Документ описывает контракт для будущей интеграции и для закрытого тестового окружения.
- Гостевые покупки (секрет заказа `access_token`) остаются основным сценарием и **не зависят** от аккаунтов.

## 1. Два независимых механизма доступа

| | Секрет заказа `access_token` (PR3) | Сессия аккаунта (PR6) |
|---|---|---|
| Что даёт | доступ к **одному** заказу: `checkout`, `status`, `delivery` | профиль, список своих заказов, привязку новых заказов |
| Где хранится | у frontend (например, `sessionStorage`) — сервер показывает его один раз | только в cookie `__Host-ds_session`, которую ставит сервер; JavaScript её **не видит** |
| Как передаётся | в **теле** POST-запроса | браузером автоматически (`credentials: "include"`) |

Сессия аккаунта **не заменяет** `access_token`: даже для заказа, привязанного к аккаунту, `checkout`, `status` и `delivery` по-прежнему требуют `access_token` из ответа на создание заказа.

## 2. Сессия: cookie `__Host-ds_session`

- Ставит **только сервер** — после успешной регистрации, входа и смены пароля (`Set-Cookie`).
- Атрибуты: `Secure; HttpOnly; SameSite=Strict; Path=/`, **без `Domain`**, `Max-Age` 90 дней (это лишь верхняя граница; действительность сессии проверяет сервер при каждом запросе — она может закончиться раньше).
- **JavaScript не читает и не хранит токен сессии.** Не копируйте его в `localStorage`, переменные, логи, URL. Узнать «вошёл ли пользователь» — только запросом `GET /api/account/me`.
- **Только HTTPS.** Префикс `__Host-` и `Secure` браузер принимает только по HTTPS; послаблений для `http://localhost` нет и не будет. Для разработки нужен HTTPS-стенд (например, локальный сертификат) с адресом из списка доверенных.
- Удаление: сервер присылает `Set-Cookie` с пустым значением и `Max-Age=0` (при выходе и при `401 session_invalid`).

### SameSite=Strict и адрес frontend

Браузер отправляет cookie только в запросах **с того же сайта** (тот же регистрируемый домен, например `shop.example` и `api.shop.example`). Следствия:

- frontend и API должны быть на одном сайте; со страницы другого сайта (другой домен, `localhost` при API на реальном домене и т. п.) аккаунт работать не будет;
- переход на страницу магазина по ссылке с чужого сайта: первый запрос навигации придёт без cookie — после загрузки страницы проверяйте сессию `fetch`-запросом к `/api/account/me`.

## 3. Origin, Content-Type, CSRF

Все изменяющие запросы аккаунта (`POST`) сервер проверяет **до** любых действий с базой:

| Запрос | `Origin` из доверенного списка | `Content-Type: application/json` | `X-CSRF-Token` |
|---|---|---|---|
| `POST /api/account/register` | да | да | нет |
| `POST /api/account/login` | да | да | нет |
| `POST /api/account/logout` | да | да | **нет** |
| `POST /api/account/logout-all` | да | да | да |
| `POST /api/account/password` | да | да | да |
| `POST /api/v2/orders` **с cookie аккаунта** | да | да | да |
| `GET /api/account/me`, `/orders`, `/orders/{id}` | нет | — | нет |

- `Origin` ставит браузер сам; сервер сравнивает его **точно** со списком доверенных HTTPS-адресов (`ACCOUNT_TRUSTED_ORIGINS` в настройках backend). Никаких `*`, шаблонов, `null`, `http://`. Адрес вашей страницы должен быть добавлен в список на backend.
- `Content-Type` — `application/json` (допустимо `application/json; charset=utf-8`). Даже если тело не нужно (`logout`, `logout-all`), отправляйте `{}` с этим заголовком.
- **CSRF-токен** берётся **только** из ответа `GET /api/account/me` (поле `csrf_token`; отдельного маршрута нет). Токен привязан к текущей сессии: после входа, регистрации и смены пароля сессия новая — снова запросите `/me`. Храните токен в памяти страницы, не в `localStorage`, не в URL.
- Отказ любой из этих проверок — `403 csrf_failed`. Причина отказа клиенту не сообщается; cookie при этом **не удаляется**.

## 4. Формат ответов

- Успех — JSON, описанный у каждого маршрута.
- Ошибка — `{"detail": {"code": "...", "message": "...", "field": "..."}}`; `field` есть только у ошибок конкретного поля (`email`, `password`, `current_password`, `new_password`). Ориентируйтесь на `code`, `message` — текст для человека (на русском).
- Введённые значения (email, пароль) в ответах **не повторяются**.
- Все ответы раздела аккаунтов и заказов с cookie — с `Cache-Control: no-store`.
- `429` и `503` приходят с заголовком `Retry-After` (секунды) — повторяйте не раньше.

Общие ошибки любого `POST /api/account/*`:

| Статус | `code` | Когда | Что делать |
|---|---|---|---|
| 403 | `csrf_failed` | неверный `Origin`, `Content-Type` или CSRF | ошибка интеграции; для запросов с CSRF — запросить `/me` и повторить один раз |
| 413 | `payload_too_large` | тело больше 4 КиБ | ошибка клиента |
| 422 | `invalid_request` | тело не JSON-объект | ошибка клиента |
| 429 | `rate_limited` | превышен лимит частоты | подождать `Retry-After` |
| 503 | `busy`, `service_unavailable` | сервер занят / временный сбой | подождать `Retry-After` и повторить |
| 500 | `internal_error` | внутренняя ошибка | общее сообщение об ошибке |
| 404 | — (`{"detail": "Not Found"}`) | аккаунты выключены | скрыть интерфейс аккаунта |

Лимиты частоты (на IP): регистрация — 1 в минуту, вход — 10, смена пароля — 5; дополнительно общий лимит входов и регистраций на процесс сервера. Отказы `403`/`422` лимит не расходуют.

## 5. Маршруты `/api/account/*`

### 5.1. `POST /api/account/register` — регистрация

Тело: `{"email": "<адрес>", "password": "<пароль>"}`.

- Email — только ASCII, до 254 символов после обрезки пробелов; регистр не важен (сервер приводит к нижнему). Адреса с не-ASCII символами (кириллические домены и т. п.) не принимаются.
- Пароль — 10–128 символов (не более 512 байт в UTF-8), без управляющих символов. Пробелы и любые печатные символы допустимы.

| Статус | Ответ | Действие frontend |
|---|---|---|
| **201** | `{"result": "created"}` + `Set-Cookie` | пользователь вошёл; запросить `/me` |
| 403 | `registration_closed` | показать «регистрация временно закрыта» |
| 409 | `email_taken` | предложить войти |
| 422 | `email_invalid`, `email_reserved` (`field: "email"`) | подсветить поле email |
| 422 | `password_too_short`, `password_too_long`, `password_forbidden_character`, `password_invalid` (`field: "password"`) | подсветить поле пароля |

### 5.2. `POST /api/account/login` — вход

Тело: `{"email": "...", "password": "..."}`.

| Статус | Ответ | Действие frontend |
|---|---|---|
| **200** | `{"result": "ok"}` + `Set-Cookie` | запросить `/me` (новая сессия — новый CSRF) |
| 401 | `invalid_credentials` | «неверный адрес или пароль» — без уточнения, что именно неверно |
| 422 | ошибки формата `email`/`password` | подсветить поле |
| 429 | `rate_limited` | подождать `Retry-After` (срок может быть до 15 минут) |

Неизвестный адрес, неверный пароль и отключённый аккаунт неотличимы — это намеренно. Если браузер уже прислал cookie сессии того же покупателя, прежняя сессия отзывается.

### 5.3. `POST /api/account/logout` — выход

Тело: `{}`. CSRF не нужен, `Origin` и `Content-Type` — нужны.

- **200** `{"result": "ok"}`, cookie удаляется **всегда**, когда проверка `Origin`/`Content-Type` пройдена — даже если сессии уже не было или база ответила ошибкой (`503`/`500` тоже приходят с удалением cookie).
- **403** `csrf_failed` — запрос не с доверенного адреса; cookie не трогается.
- **413** `payload_too_large` (тело больше 4 КиБ) и **429** `rate_limited` (общий лимит запросов с адреса) отвечает слой ограничений **до** маршрута выхода: выход не выполнен, cookie **не** удаляется. Отправляйте `{}` и при `429` повторите после `Retry-After`.

После выхода считайте пользователя вышедшим только при статусах, которые возвращает сам маршрут выхода (`200`, `503`, `500`); при `403`, `413` и `429` выход не выполнен.

- **200** — сессия отозвана на сервере (или её уже не было).
- **503** и **500** — cookie удалена, но отзыв серверной сессии **не гарантирован**: для frontend это локальный выход, а не подтверждённый серверный. Повторять выход не нужно — без cookie он ничего не отзовёт; несмотря на общее правило для `503`, этот ответ не повторяйте. Серверная сессия, если не отозвана, истечёт по своему сроку. Если нужно гарантированно закрыть все сессии, после восстановления сервиса пользователь входит снова и выполняет `logout-all` (§5.4).

### 5.4. `POST /api/account/logout-all` — выйти на всех устройствах

Тело: `{}`, заголовок `X-CSRF-Token`.

| Статус | Ответ | Действие |
|---|---|---|
| **200** | `{"result": "ok"}`, cookie удалена | все сессии покупателя отозваны |
| 401 | `session_invalid`, cookie удалена | сессия уже недействительна — показать вход |
| 403 | `csrf_failed` | обновить CSRF через `/me`; **без cookie сессии** ответ тоже `403 csrf_failed` (не `401`) — показать вход |
| 503 | `busy` | повторить после `Retry-After` |
| 404 | `{"detail": "Not Found"}` | аккаунты выключены (в том числе только в базе) — скрыть интерфейс аккаунта |

### 5.5. `POST /api/account/password` — смена пароля

Тело: `{"current_password": "...", "new_password": "..."}`, заголовок `X-CSRF-Token`.

| Статус | Ответ | Действие |
|---|---|---|
| **200** | `{"result": "ok"}` + **новая** `Set-Cookie` | остальные сессии покупателя отозваны; запросить `/me` за новым CSRF |
| 403 | `invalid_password` | «неверный текущий пароль» |
| 403 | `csrf_failed` | обновить CSRF и повторить; **без cookie сессии** ответ тоже `403 csrf_failed` (не `401`) — показать вход |
| 401 | `session_invalid`, cookie удалена | показать вход |
| 422 | ошибки пароля с `field: "current_password"` или `"new_password"` | подсветить поле |
| 429 | `rate_limited` | подождать |
| 404 | `{"detail": "Not Found"}` | аккаунты выключены (в том числе только в базе) — скрыть интерфейс аккаунта |

### 5.6. `GET /api/account/me` — профиль и CSRF-токен

Без тела. Ответ **200**:

```json
{"email": "buyer@example.test", "created_at": "2026-10-17T12:00:00+03:00", "csrf_token": "<43 символа>"}
```

- **401** `session_invalid` + удаление cookie — не вошёл или сессия истекла/отозвана.
- Успешный запрос продлевает срок простоя сессии, но не чаще одного раза за `session_touch_interval` (настройка backend, по умолчанию 10 минут); более частые запросы срок не меняют.

### 5.7. `GET /api/account/orders` — мои заказы

Ответ **200**: `{"orders": [{"id": 41, "status": "pending", "created_at": "...", "total_minor": 10000, "currency": "RUB"}]}`. Только заказы, привязанные к этому аккаунту; суммы — в минимальных единицах (`total_minor`) и валюте. `401 session_invalid` — как у `/me`.

### 5.8. `GET /api/account/orders/{order_id}` — один мой заказ

Ответ **200**:

```json
{
  "order": {"id": 41, "status": "pending", "created_at": "...", "total_minor": 10000, "currency": "RUB"},
  "items": [{"product_id": 5, "name": "Ключ", "product_type": "digital_key", "quantity": 1,
             "unit_price_minor": 10000, "line_total_minor": 10000}]
}
```

- **404** `order_not_found` — чужой, несуществующий или неверный номер (неотличимы).
- **401** `session_invalid` — сессия недействительна (cookie удаляется).
- `access_token` и ключи в этом ответе **нет**: оплата и выдача — прежними маршрутами с `access_token`.

## 6. Оформление заказа: `POST /api/v2/orders`

Тело и ответ прежние (см. FRONTEND_API.md): `{"items": [{"product_id": 5, "quantity": 1}]}` → `201 {"order": {...}, "access_token": "..."}`.

### Гость (без cookie аккаунта)

- Работает как раньше, без `Origin`/CSRF-требований, тело ответа строго `{"order", "access_token"}`.
- **Отправляйте с `credentials: "omit"`**, чтобы случайно не приложить cookie аккаунта: запрос с cookie уже не гостевой и требует CSRF.

### С аккаунтом (cookie `__Host-ds_session`)

Нужны `credentials: "include"`, доверенный `Origin`, `Content-Type: application/json` и `X-CSRF-Token`. Заказ, его `access_token` и привязка к аккаунту создаются **в одной транзакции**: либо всё, либо ничего.

| Статус | Ответ | Действие |
|---|---|---|
| **201** | `{"order": {...}, "access_token": "...", "account_linked": true}` | сохранить `access_token` (нужен для оплаты), заказ появится в `/api/account/orders` |
| 401 | `session_invalid`, cookie удалена | **заказ не создан**; предложить войти снова и повторить оформление (или оформить как гость) |
| 403 | `csrf_failed` | **заказ не создан**, cookie не тронута; запросить `/me` и повторить |
| 422 | `invalid_request` | неверное тело (лишние поля, пустой список, не JSON); введённые значения в ответе не повторяются |
| 404 / 409 | `product_not_found`, `insufficient_stock` и др. | как у гостя (`{"detail": {"code", "message", "product_ids"}}`) |
| 413 / 429 | `payload_too_large` / `rate_limited` | как у гостя; общий лимит — 10 заказов в минуту с адреса |
| 500 | `internal_error` | общая ошибка; гостевой заказ взамен **не** создаётся |

Отличие `422`: у гостя — прежний стандартный ответ FastAPI (`{"detail": [ ... ]}`), с cookie — `{"detail": {"code": "invalid_request", ...}}` без деталей. Если CSRF неверен, сначала приходит `403`, даже при неверном теле.

`account_linked` есть **только** в ответе с привязкой; у гостя этого поля нет.

### Что не изменилось

- `checkout`, `status`, `delivery` — только с `access_token` в теле, cookie аккаунта на них не влияет.
- API v1 (`/products`, `/orders`) не менялся и cookie не читает.

## 7. CORS

| | Аккаунты выключены | Аккаунты включены |
|---|---|---|
| `allow_credentials` | `false` (как раньше) | `true` — только для адресов из `CORS_ALLOWED_ORIGINS` |
| Разрешённые адреса | `CORS_ALLOWED_ORIGINS` или адреса разработки `http://localhost…` | **только** точные HTTPS-адреса, каждый из которых есть и в `ACCOUNT_TRUSTED_ORIGINS`; адреса разработки по умолчанию не применяются |
| Заголовки | `Content-Type` | `Content-Type`, `X-CSRF-Token` |
| Методы | `GET`, `POST` | `GET`, `POST` |

Если frontend и API на одном адресе, `CORS_ALLOWED_ORIGINS` пуст, но адрес страницы всё равно должен быть в `ACCOUNT_TRUSTED_ORIGINS` (браузер присылает `Origin` и на запросы своего сайта). Работа со страницы на произвольном `localhost` или на другом сайте **не поддерживается**.

## 8. Примеры (fetch)

```js
const API = "https://shop.example";          // адрес API; страница — с доверенного HTTPS-адреса того же сайта

// Запрос раздела аккаунтов: cookie отправляет браузер, токен сессии JavaScript не видит.
async function accountFetch(path, { method = "GET", body, csrf } = {}) {
  const headers = {};
  if (method !== "GET") headers["Content-Type"] = "application/json";
  if (csrf) headers["X-CSRF-Token"] = csrf;
  const response = await fetch(API + path, {
    method, headers, credentials: "include", cache: "no-store",
    body: method === "GET" ? undefined : JSON.stringify(body ?? {}),
  });
  const data = await response.json().catch(() => null);
  return { status: response.status, data, code: data?.detail?.code };
}

let csrfToken = null;                         // только в памяти страницы

async function loadProfile() {               // кто вошёл + свежий CSRF
  const r = await accountFetch("/api/account/me");
  if (r.status === 200) { csrfToken = r.data.csrf_token; return r.data; }
  csrfToken = null;
  return null;                                // 401 — не вошёл; 404 — аккаунты выключены
}
```

Регистрация и вход:

```js
await accountFetch("/api/account/register", { method: "POST", body: { email, password } }); // 201 / 403 / 409 / 422
const login = await accountFetch("/api/account/login", { method: "POST", body: { email, password } });
if (login.status === 200) await loadProfile();   // новая сессия — новый CSRF
```

Смена пароля, выход:

```js
const changed = await accountFetch("/api/account/password", {
  method: "POST", csrf: csrfToken, body: { current_password: oldPassword, new_password: newPassword } });
if (changed.status === 200) await loadProfile();

await accountFetch("/api/account/logout", { method: "POST" });                    // CSRF не нужен
await accountFetch("/api/account/logout-all", { method: "POST", csrf: csrfToken }); // нужен
```

Заказы аккаунта:

```js
const { data: list } = await accountFetch("/api/account/orders");          // { orders: [...] }
const one = await accountFetch(`/api/account/orders/${encodeURIComponent(orderId)}`); // 200 / 404 / 401
```

Гостевой заказ — **без** cookie:

```js
const guest = await fetch(API + "/api/v2/orders", {
  method: "POST", credentials: "omit",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ items: [{ product_id: 5, quantity: 1 }] }),
});
const { order, access_token } = await guest.json();       // access_token — сохранить для оплаты
```

Заказ с привязкой к аккаунту:

```js
const linked = await accountFetch("/api/v2/orders", {
  method: "POST", csrf: csrfToken, body: { items: [{ product_id: 5, quantity: 1 }] } });
if (linked.status === 201) {
  const { order, access_token, account_linked } = linked.data;   // account_linked === true
} else if (linked.code === "session_invalid") {
  // заказ не создан, cookie удалена: предложить вход (или оформление как гость)
} else if (linked.code === "csrf_failed") {
  await loadProfile();                                    // обновить CSRF и повторить один раз
}
```

## 9. Типовые сценарии

| Ситуация | Что видит frontend | Что делать |
|---|---|---|
| Не вошёл | `/me` → `401 session_invalid` | показать «Войти»; покупки — как гость (`credentials: "omit"`) |
| Сессия истекла или отозвана | любой запрос аккаунта или заказ с cookie → `401 session_invalid`, cookie удалена | сбросить состояние и CSRF, предложить вход; заказ при этом не создан |
| Регистрация закрыта | `register` → `403 registration_closed` | сообщение «регистрация временно закрыта», вход оставить |
| CSRF не прошёл | `403 csrf_failed` | запросить `/me`, повторить один раз; повторный отказ — ошибка интеграции (адрес страницы не в доверенном списке?) |
| Чужой или несуществующий заказ | `/orders/{id}` → `404 order_not_found` | «заказ не найден» — без различения |
| Аккаунты выключены | `/api/account/*` → `404 {"detail": "Not Found"}` | скрыть вход, регистрацию и личный кабинет; гостевые покупки работают |
| Аккаунты выключены только в базе (переходное состояние) | `register`, `login`, `logout-all`, `password` → `404 {"detail": "Not Found"}`; `/me`, заказы аккаунта и заказ с cookie → `401 session_invalid` (сессии на время выключения недействительны); `logout` → `200` с удалением cookie | как «аккаунты выключены»: скрыть интерфейс аккаунта, покупки — как гость |

## 10. Безопасность на стороне клиента

- Пароль, токен сессии, CSRF-токен и `access_token` **никогда** не попадают в URL, query, логи, сообщения об ошибках, аналитические события и системы мониторинга.
- Токен сессии JavaScript не получает вовсе — не пытайтесь его прочитать или передать вручную.
- CSRF-токен — только в памяти страницы и только в заголовке `X-CSRF-Token`.
- Ответы аккаунта и заказов с cookie не кэшируются (`no-store`) — не сохраняйте их в собственных кэшах (Service Worker и т. п.).
- Для гостевых запросов — `credentials: "omit"`, для запросов аккаунта — `credentials: "include"`.
