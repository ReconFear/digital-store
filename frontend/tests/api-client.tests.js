// Тесты JS-клиента. Не обращаются к настоящему серверу: fetch подменяется.
// Запуск: в браузере — frontend/tests/index.html; в Node.js 22.7+ — node frontend/tests/run-node.mjs

import {
  createApiClient,
  DigitalStoreApiError,
  ENDPOINTS,
  formatMoney,
  isInStock,
} from "../lib/digital-store-api.js";

const PRODUCT = { id: 1, name: "Minecraft Key", price: 999, quantity: 14, is_for_sale: true };

const RUB = (amount_minor) => ({ amount_minor, currency: "RUB", exponent: 2 });
const PRODUCT_V2 = {
  id: 1, name: "Minecraft Key", product_type: "other", inventory_mode: "tracked",
  price: RUB(99900), stock_quantity: 14, available: true,
};
const SERVICE_V2 = {
  id: 7, name: "Консультация", product_type: "service", inventory_mode: "unlimited",
  price: RUB(300050), stock_quantity: null, available: true,
};
const ORDER_V2 = {
  id: 42, status: "pending", created_at: "2026-10-13T00:00:00+00:00", total: RUB(499850),
  items: [
    { product_id: 1, name: "Minecraft Key", product_type: "other", quantity: 2, unit_price: RUB(99900), line_total: RUB(199800) },
    { product_id: 7, name: "Консультация", product_type: "service", quantity: 1, unit_price: RUB(300050), line_total: RUB(300050) },
  ],
};

function assert(condition, message) {
  if (!condition) throw new Error(message || "assertion failed");
}

function assertEqual(actual, expected, message) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${message || "не равно"}: получено ${a}, ожидалось ${e}`);
}

async function assertApiError(promiseOrFn, code) {
  try {
    await (typeof promiseOrFn === "function" ? promiseOrFn() : promiseOrFn);
  } catch (error) {
    assert(error instanceof DigitalStoreApiError, `ожидалась DigitalStoreApiError, получено ${error}`);
    assertEqual(error.code, code, "code");
    assert(typeof error.userMessage === "string" && error.userMessage.length > 0, "нет userMessage");
    return error;
  }
  throw new Error(`ожидалась ошибка ${code}, но её не было`);
}

/** Поддельный fetch: запоминает вызовы и отвечает заданным образом. */
function fakeFetch(respond) {
  const calls = [];
  const fn = async (url, init) => {
    calls.push({ url, init });
    return respond(url, init);
  };
  fn.calls = calls;
  return fn;
}

function jsonResponse(status, body) {
  return new Response(body === undefined ? "" : JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function client(fetch, extra = {}) {
  return createApiClient({ baseUrl: "http://127.0.0.1:8000", fetch, ...extra });
}

export const tests = [
  ["getProducts: возвращает products и total как есть", async () => {
    const fetch = fakeFetch(() => jsonResponse(200, { products: [PRODUCT], total: 1 }));
    const result = await client(fetch).getProducts();
    assertEqual(result, { products: [PRODUCT], total: 1 });
    assertEqual(fetch.calls[0].url, "http://127.0.0.1:8000/products");
    assertEqual(fetch.calls[0].init.method, "GET");
    assertEqual(fetch.calls[0].init.credentials, "omit");
    assertEqual(fetch.calls[0].init.headers, { Accept: "application/json" }, "GET без Content-Type (без лишнего preflight)");
  }],

  ["getProducts: пустой каталог — не ошибка", async () => {
    const fetch = fakeFetch(() => jsonResponse(200, { products: [], total: 0 }));
    assertEqual(await client(fetch).getProducts(), { products: [], total: 0 });
  }],

  ["getProducts({ availableOnly: true }) добавляет available_only=true", async () => {
    const fetch = fakeFetch(() => jsonResponse(200, { products: [], total: 0 }));
    await client(fetch).getProducts({ availableOnly: true });
    assertEqual(fetch.calls[0].url, "http://127.0.0.1:8000/products?available_only=true");
  }],

  ["getProducts: неожиданный формат -> invalid_response", async () => {
    for (const body of [{ items: [] }, { products: [{ id: 1 }], total: 1 }, [PRODUCT]]) {
      const fetch = fakeFetch(() => jsonResponse(200, body));
      await assertApiError(client(fetch).getProducts(), "invalid_response");
    }
  }],

  ["ответ не JSON -> invalid_response", async () => {
    const fetch = fakeFetch(() => new Response("<html>oops</html>", { status: 200 }));
    await assertApiError(client(fetch).getProducts(), "invalid_response");
  }],

  ["getProduct: товар по id", async () => {
    const fetch = fakeFetch(() => jsonResponse(200, PRODUCT));
    assertEqual(await client(fetch).getProduct(1), PRODUCT);
    assertEqual(fetch.calls[0].url, "http://127.0.0.1:8000/products/1");
  }],

  ["getProduct: id строкой из URL тоже принимается", async () => {
    const fetch = fakeFetch(() => jsonResponse(200, PRODUCT));
    await client(fetch).getProduct("1");
    assertEqual(fetch.calls[0].url, "http://127.0.0.1:8000/products/1");
  }],

  ["getProduct: 404 -> not_found с detail от backend", async () => {
    const fetch = fakeFetch(() => jsonResponse(404, { detail: "Product not found" }));
    const error = await assertApiError(client(fetch).getProduct(999), "not_found");
    assertEqual(error.status, 404);
    assertEqual(error.detail, "Product not found");
    assertEqual(error.retryable, false);
  }],

  ["getProduct: неверный id отклоняется без запроса к серверу", async () => {
    const fetch = fakeFetch(() => jsonResponse(200, PRODUCT));
    for (const bad of [0, -1, 1.5, NaN, "abc", "1/../orders", "1?x=1", null, undefined, "", " 1"]) {
      await assertApiError(client(fetch).getProduct(bad), "invalid_argument");
    }
    assertEqual(fetch.calls.length, 0, "запросов быть не должно");
  }],

  ["сервер недоступен / CORS -> network, можно повторить", async () => {
    const fetch = fakeFetch(() => { throw new TypeError("Failed to fetch"); });
    const error = await assertApiError(client(fetch).getProducts(), "network");
    assertEqual(error.status, null);
    assertEqual(error.retryable, true);
  }],

  ["таймаут -> timeout", async () => {
    const fetch = (url, init) => new Promise((resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    });
    const error = await assertApiError(client(fetch, { timeoutMs: 20 }).getProducts(), "timeout");
    assertEqual(error.retryable, true);
  }],

  ["отмена через AbortSignal -> aborted", async () => {
    const fetch = (url, init) => new Promise((resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    });
    const controller = new AbortController();
    const promise = client(fetch).getProducts({ signal: controller.signal });
    controller.abort();
    await assertApiError(promise, "aborted");
  }],

  ["HTTP-статусы превращаются в понятные коды", async () => {
    const cases = [[403, "forbidden"], [409, "unavailable"], [422, "validation"], [500, "server"], [503, "server"], [418, "http"]];
    for (const [status, code] of cases) {
      const fetch = fakeFetch(() => jsonResponse(status, { detail: "x" }));
      const error = await assertApiError(client(fetch).getProducts(), code);
      assertEqual(error.status, status);
    }
  }],

  ["422: detail-массив FastAPI сохраняется", async () => {
    const detail = [{ type: "greater_than", loc: ["body", "product_id"], msg: "Input should be greater than 0" }];
    const fetch = fakeFetch(() => jsonResponse(422, { detail }));
    const error = await assertApiError(client(fetch, { allowOrderCreation: true }).createOrder(1), "validation");
    assertEqual(error.detail, detail);
  }],

  ["ошибка без JSON-тела не ломает клиент", async () => {
    const fetch = fakeFetch(() => new Response("Internal Server Error", { status: 500 }));
    const error = await assertApiError(client(fetch).getProducts(), "server");
    assertEqual(error.detail, null);
  }],

  ["createOrder по умолчанию отключён и не обращается к серверу", async () => {
    const fetch = fakeFetch(() => jsonResponse(200, {}));
    await assertApiError(client(fetch).createOrder(1), "orders_disabled");
    assertEqual(fetch.calls.length, 0);
  }],

  ["createOrder: точный контракт POST /orders", async () => {
    const order = { id: 10, product_id: 1, price: 999, status: "pending" };
    const fetch = fakeFetch(() => jsonResponse(200, { message: "Order created", order }));
    const result = await client(fetch, { allowOrderCreation: true }).createOrder(1);
    assertEqual(result, { message: "Order created", order });
    const { url, init } = fetch.calls[0];
    assertEqual(url, "http://127.0.0.1:8000/orders");
    assertEqual(init.method, "POST");
    assertEqual(init.headers["Content-Type"], "application/json");
    assertEqual(init.body, JSON.stringify({ product_id: 1 }));
  }],

  ["createOrder: 409 -> unavailable", async () => {
    const fetch = fakeFetch(() => jsonResponse(409, { detail: "Product unavailable" }));
    const error = await assertApiError(client(fetch, { allowOrderCreation: true }).createOrder(2), "unavailable");
    assertEqual(error.detail, "Product unavailable");
  }],

  ["createOrder: неверный productId отклоняется без запроса", async () => {
    const fetch = fakeFetch(() => jsonResponse(200, {}));
    for (const bad of [0, -5, 1.2, "1", null]) {
      await assertApiError(client(fetch, { allowOrderCreation: true }).createOrder(bad), "invalid_argument");
    }
    assertEqual(fetch.calls.length, 0);
  }],

  ["baseUrl: завершающий слеш убирается, неверные адреса отклоняются", async () => {
    const fetch = fakeFetch(() => jsonResponse(200, { products: [], total: 0 }));
    const api = createApiClient({ baseUrl: "http://localhost:8000/", fetch });
    assertEqual(api.baseUrl, "http://localhost:8000");
    await api.getProducts();
    assertEqual(fetch.calls[0].url, "http://localhost:8000/products");

    for (const bad of ["", "localhost:8000", "javascript:alert(1)", "ftp://x", "http://x/?a=1", "http://u:p@x"]) {
      let thrown = false;
      try { createApiClient({ baseUrl: bad, fetch }); } catch (e) { thrown = e instanceof TypeError; }
      assert(thrown, `baseUrl ${JSON.stringify(bad)} должен быть отклонён`);
    }
  }],

  ["baseUrl с путём (API за прокси /api) поддерживается", async () => {
    const fetch = fakeFetch(() => jsonResponse(200, PRODUCT));
    await createApiClient({ baseUrl: "https://shop.example.com/api/", fetch }).getProduct(1);
    assertEqual(fetch.calls[0].url, "https://shop.example.com/api/products/1");
  }],

  ["v2: getProductsV2 принимает товары без учёта остатка (stock_quantity: null)", async () => {
    const fetch = fakeFetch(() => jsonResponse(200, { products: [PRODUCT_V2, SERVICE_V2], total: 2 }));
    const result = await client(fetch).getProductsV2({ availableOnly: true });
    assertEqual(result.products, [PRODUCT_V2, SERVICE_V2]);
    assertEqual(fetch.calls[0].url, "http://127.0.0.1:8000/api/v2/products?available_only=true");
  }],

  ["v2: getProductsV2 отклоняет цену не в целых минимальных единицах", async () => {
    for (const price of [{ amount_minor: 999.5, currency: "RUB", exponent: 2 }, { amount_minor: "99900", currency: "RUB", exponent: 2 }, 999]) {
      const fetch = fakeFetch(() => jsonResponse(200, { products: [{ ...PRODUCT_V2, price }], total: 1 }));
      await assertApiError(client(fetch).getProductsV2(), "invalid_response");
    }
  }],

  ["v2: getProductV2 и not_found со структурированным detail", async () => {
    const ok = fakeFetch(() => jsonResponse(200, SERVICE_V2));
    assertEqual(await client(ok).getProductV2("7"), SERVICE_V2);
    assertEqual(ok.calls[0].url, "http://127.0.0.1:8000/api/v2/products/7");

    const detail = { code: "product_not_found", message: "Товар не найден или не продаётся.", product_ids: [5] };
    const missing = fakeFetch(() => jsonResponse(404, { detail }));
    const error = await assertApiError(client(missing).getProductV2(5), "not_found");
    assertEqual(error.detail, detail);
  }],

  ["v2: createOrderV2 отправляет только product_id и quantity", async () => {
    const fetch = fakeFetch(() => jsonResponse(201, { order: ORDER_V2 }));
    const items = [{ product_id: 1, quantity: 2, price: 1, name: "подделка" }, { product_id: 7, quantity: 1 }];
    const result = await client(fetch, { allowOrderCreation: true }).createOrderV2(items);
    assertEqual(result, { order: ORDER_V2 });
    assertEqual(fetch.calls[0].url, "http://127.0.0.1:8000/api/v2/orders");
    assertEqual(fetch.calls[0].init.method, "POST");
    assertEqual(fetch.calls[0].init.body, JSON.stringify({ items: [{ product_id: 1, quantity: 2 }, { product_id: 7, quantity: 1 }] }));
  }],

  ["v2: createOrderV2 выключен по умолчанию и проверяет позиции до запроса", async () => {
    const fetch = fakeFetch(() => jsonResponse(201, { order: ORDER_V2 }));
    await assertApiError(client(fetch).createOrderV2([{ product_id: 1, quantity: 1 }]), "orders_disabled");
    const api = client(fetch, { allowOrderCreation: true });
    for (const bad of [[], null, [{ product_id: 1 }], [{ product_id: 1, quantity: 0 }], [{ product_id: "1", quantity: 1 }], [{ product_id: 1, quantity: 1.5 }]]) {
      await assertApiError(api.createOrderV2(bad), "invalid_argument");
    }
    assertEqual(fetch.calls.length, 0);
  }],

  ["v2: коды бизнес-ошибок из detail.code", async () => {
    for (const [status, code] of [[409, "insufficient_stock"], [409, "mixed_currency"], [409, "unsupported_currency"]]) {
      const fetch = fakeFetch(() => jsonResponse(status, { detail: { code, message: "x", product_ids: [1] } }));
      const error = await assertApiError(client(fetch, { allowOrderCreation: true }).createOrderV2([{ product_id: 1, quantity: 1 }]), code);
      assertEqual(error.detail.product_ids, [1]);
    }
  }],

  ["v2: неожиданный формат заказа -> invalid_response", async () => {
    const fetch = fakeFetch(() => jsonResponse(201, { order: { ...ORDER_V2, total: 4998.5 } }));
    await assertApiError(client(fetch, { allowOrderCreation: true }).createOrderV2([{ product_id: 1, quantity: 1 }]), "invalid_response");
  }],

  ["formatMoney: копейки без float", async () => {
    const plain = (text) => text.replace(/\s/g, " ");
    assertEqual(plain(formatMoney(RUB(99950))), "999,50 ₽");
    assertEqual(plain(formatMoney(RUB(99900))), "999 ₽");
    assertEqual(plain(formatMoney(RUB(5))), "0,05 ₽");
    assertEqual(plain(formatMoney(RUB(0))), "0 ₽");
    assertEqual(plain(formatMoney(RUB(123456789012))), "1 234 567 890,12 ₽");
    let thrown = false;
    try { formatMoney({ amount_minor: 1.5, currency: "RUB", exponent: 2 }); } catch { thrown = true; }
    assert(thrown, "дробные копейки должны отклоняться");
  }],

  ["isInStock и список эндпоинтов", async () => {
    assertEqual(isInStock(PRODUCT), true);
    assertEqual(isInStock({ ...PRODUCT, quantity: 0 }), false);
    assertEqual(isInStock({ ...PRODUCT, is_for_sale: false }), false);
    assertEqual(isInStock(null), false);
    assert(Object.isFrozen(ENDPOINTS), "ENDPOINTS должен быть неизменяемым");
  }],
];

/** Запускает все тесты; report(name, error|null) вызывается для каждого. */
export async function runTests(report) {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      report(name, null);
    } catch (error) {
      failed += 1;
      report(name, error);
    }
  }
  return { total: tests.length, failed };
}
