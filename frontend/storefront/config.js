// Публичная конфигурация витрины. Здесь НЕТ и не должно быть секретов:
// файл целиком отдаётся браузеру любого посетителя.

function deepFreeze(value) {
  for (const item of Object.values(value)) {
    if (item && typeof item === "object") deepFreeze(item);
  }
  return Object.freeze(value);
}

export const CONFIG = deepFreeze({
  // Адрес backend для разработки. Должен совпадать с connect-src в index.html.
  apiBaseUrl: "http://127.0.0.1:8000",
  requestTimeoutMs: 10000,
  // Сколько миллисекунд загруженный каталог считается свежим (поиск и фильтры работают по нему).
  catalogCacheMs: 60000,
  // Оформление оплаты на сервере может длиться дольше обычного запроса (повторы к ЮKassa).
  checkoutTimeoutMs: 45000,
  // Домены страницы оплаты, на которые разрешён переход (зеркало YOOKASSA_CONFIRMATION_HOSTS).
  paymentHosts: ["yoomoney.ru", "yookassa.ru"],
  // Что из функций уже включено. T3: каталог и поиск; корзина, оформление и выдача — нет.
  features: {
    catalog: true,
    search: true,
    cart: true,
    checkout: false,
    delivery: false,
    accounts: false,
    debug: false,
  },
});
