// Поддельные ответы API для тестов каталога (T3). Сеть не используется, заказы не создаются.
// Товары ниже — ТОЛЬКО тестовые данные: в витрине их нет и быть не должно.

/** Тестовый товар API v2 со всеми обязательными полями контракта. */
export function testProduct(overrides = {}) {
  return {
    id: 1,
    name: "Тестовый товар",
    product_type: "digital_key",
    inventory_mode: "tracked",
    price: { amount_minor: 10000, currency: "RUB", exponent: 2 },
    stock_quantity: 5,
    available: true,
    ...overrides,
  };
}

export function catalogBody(products) {
  return { products, total: products.length };
}

export function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => (body === undefined ? "" : JSON.stringify(body)),
  };
}

export function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

/** Дождаться, пока выполнятся все отложенные промисы и таймеры нулевой длительности. */
export function flush() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

/**
 * Поддельный fetch: записывает каждый вызов { url, method, body, signal } и отвечает через
 * respond(call, index). respond может вернуть ответ, промис ответа или бросить ошибку.
 * Отмена signal отклоняет ожидающий ответ, как настоящий fetch.
 */
export function fakeFetch(respond) {
  const calls = [];
  async function fetch(url, init = {}) {
    const call = { url, method: init.method ?? "GET", body: init.body, signal: init.signal };
    calls.push(call);
    const pending = Promise.resolve().then(() => respond(call, calls.length - 1));
    if (!init.signal) return pending;
    return new Promise((resolve, reject) => {
      const onAbort = () => {
        const error = new Error("The operation was aborted.");
        error.name = "AbortError";
        reject(error);
      };
      if (init.signal.aborted) return onAbort();
      init.signal.addEventListener("abort", onAbort, { once: true });
      pending.then(resolve, reject);
    });
  }
  fetch.calls = calls;
  return fetch;
}

/** Ошибка сети, как у браузера при выключенном backend: fetch бросает TypeError. */
export function networkFailure() {
  throw new TypeError("Failed to fetch");
}

/** Поддельный api (вместо createStoreApi): getProductsV2 отдаёт заранее заданные ответы. */
export function fakeCatalogApi(...responses) {
  const calls = [];
  return {
    calls,
    getProductsV2({ signal } = {}) {
      calls.push({ signal });
      const next = responses[Math.min(calls.length - 1, responses.length - 1)];
      return typeof next === "function" ? next(signal) : next;
    },
  };
}
