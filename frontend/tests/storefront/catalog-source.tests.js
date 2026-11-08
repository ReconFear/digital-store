// Тесты js/core/api.js и js/catalog/source.js: только GET каталога, кэш, общий запрос,
// отмена при уходе со страницы, защита от устаревшего ответа, ошибки без кэша.
// Настоящий клиент frontend/lib/digital-store-api.js работает поверх поддельного fetch.

import { createStoreApi } from "../../storefront/js/core/api.js";
import { createCatalogSource, isAbortError } from "../../storefront/js/catalog/source.js";
import {
  catalogBody, deferred, fakeCatalogApi, fakeFetch, flush, jsonResponse, networkFailure, testProduct,
} from "./fake-api.js";
import { assert, assertEqual } from "./assert.js";

const CONFIG = Object.freeze({ apiBaseUrl: "http://127.0.0.1:8000", requestTimeoutMs: 10000 });
const CATALOG_URL = "http://127.0.0.1:8000/api/v2/products";

function setup(respond, { ttlMs = 60000 } = {}) {
  const fetch = fakeFetch(respond);
  let time = 0;
  const source = createCatalogSource({ api: createStoreApi(CONFIG, { fetch }), ttlMs, now: () => time });
  return { fetch, source, tick: (ms) => { time += ms; } };
}

const ok = (products) => () => jsonResponse(200, catalogBody(products));

async function rejectsWith(promise) {
  try {
    await promise;
  } catch (error) {
    return error;
  }
  throw new Error("ожидалась ошибка");
}

export const tests = [
  ["catalog-source: клиент витрины умеет только читать каталог", () => {
    const api = createStoreApi(CONFIG, { fetch: fakeFetch(ok([])) });
    assertEqual(Object.keys(api).sort(), ["baseUrl", "getProductV2", "getProductsV2"]);
    assertEqual(api.baseUrl, "http://127.0.0.1:8000");
  }],

  ["catalog-source: один GET /api/v2/products без тела и без параметров", async () => {
    const { fetch, source } = setup(ok([testProduct({ id: 1 })]));
    const data = await source.load();
    assertEqual(fetch.calls.length, 1);
    assertEqual(fetch.calls[0].url, CATALOG_URL);
    assertEqual(fetch.calls[0].method, "GET");
    assertEqual(fetch.calls[0].body, undefined);
    assertEqual(data.total, 1);
    assertEqual(data.products[0].id, 1);
  }],

  ["catalog-source: берутся только поля контракта; данные неизменяемы", async () => {
    const extra = { ...testProduct({ id: 2 }), description: "не из контракта", image_url: "https://example.test/x.png" };
    const { source } = setup(() => jsonResponse(200, { products: [extra], total: 1 }));
    const { products } = await source.load();
    assertEqual(Object.keys(products[0]).sort(),
      ["available", "id", "inventory_mode", "name", "price", "product_type", "stock_quantity"]);
    assert(Object.isFrozen(products) && Object.isFrozen(products[0]) && Object.isFrozen(products[0].price));
  }],

  ["catalog-source: повторная загрузка из кэша — без нового запроса; после ttl — новый GET", async () => {
    const { fetch, source, tick } = setup(ok([testProduct()]), { ttlMs: 1000 });
    const first = await source.load();
    tick(999);
    assert(source.peek() === first, "свежий кэш доступен сразу");
    assert((await source.load()) === first);
    assertEqual(fetch.calls.length, 1);
    tick(1);
    assertEqual(source.peek(), null, "устаревший кэш не отдаётся");
    await source.load();
    assertEqual(fetch.calls.length, 2);
  }],

  ["catalog-source: одновременные загрузки ждут один запрос", async () => {
    const gate = deferred();
    const { fetch, source } = setup(() => gate.promise);
    const a = source.load();
    const b = source.load();
    const c = source.load({ force: true });
    await flush();
    assertEqual(fetch.calls.length, 1);
    gate.resolve(jsonResponse(200, catalogBody([testProduct()])));
    const [da, db, dc] = await Promise.all([a, b, c]);
    assert(da === db && db === dc);
  }],

  ["catalog-source: уход одного экрана не отменяет запрос для другого", async () => {
    const gate = deferred();
    const { fetch, source } = setup(() => gate.promise);
    const leaving = new AbortController();
    const first = source.load({ signal: leaving.signal });
    const second = source.load();
    leaving.abort();
    assert(isAbortError(await rejectsWith(first)), "ушедший экран получает AbortError");
    await flush();
    assertEqual(fetch.calls[0].signal.aborted, false, "запрос ещё нужен второму экрану");
    gate.resolve(jsonResponse(200, catalogBody([testProduct({ id: 7 })])));
    assertEqual((await second).products[0].id, 7);
  }],

  ["catalog-source: запрос отменяется, когда его больше никто не ждёт", async () => {
    const { fetch, source } = setup(() => deferred().promise);
    const leaving = new AbortController();
    const pending = source.load({ signal: leaving.signal });
    await flush();
    leaving.abort();
    await rejectsWith(pending);
    await flush();
    assertEqual(fetch.calls[0].signal.aborted, true);
    assertEqual(source.pending, false);
    assertEqual(source.peek(), null, "отменённый запрос ничего не кладёт в кэш");
  }],

  ["catalog-source: переход «главная -> каталог» во время загрузки — один запрос", async () => {
    const gate = deferred();
    const { fetch, source } = setup(() => gate.promise);
    const home = new AbortController();
    const homeLoad = source.load({ signal: home.signal });
    // Маршрутизатор отменяет старый экран и сразу (синхронно) рисует новый.
    home.abort();
    const catalogLoad = source.load({ signal: new AbortController().signal });
    await rejectsWith(homeLoad);
    await flush();
    assertEqual(fetch.calls.length, 1, "второй запрос не нужен");
    assertEqual(fetch.calls[0].signal.aborted, false);
    gate.resolve(jsonResponse(200, catalogBody([testProduct()])));
    assertEqual((await catalogLoad).total, 1);
  }],

  ["catalog-source: уже отменённый signal — без запроса", async () => {
    const { fetch, source } = setup(ok([]));
    const controller = new AbortController();
    controller.abort();
    assert(isAbortError(await rejectsWith(source.load({ signal: controller.signal }))));
    await flush();
    assertEqual(fetch.calls.length, 0);
  }],

  ["catalog-source: устаревший ответ не перезаписывает кэш более нового запроса", async () => {
    const old = deferred();
    const fresh = deferred();
    // Поддельный api игнорирует отмену — как ответ, который успел прийти до abort.
    const api = fakeCatalogApi(() => old.promise, () => fresh.promise);
    const source = createCatalogSource({ api });
    const leaving = new AbortController();
    const first = source.load({ signal: leaving.signal });
    await flush();
    leaving.abort();
    await rejectsWith(first);
    await flush();
    const second = source.load();
    await flush();
    assertEqual(api.calls.length, 2);
    fresh.resolve(catalogBody([testProduct({ id: 2, name: "Новый ответ" })]));
    await second;
    old.resolve(catalogBody([testProduct({ id: 1, name: "Старый ответ" })]));
    await flush();
    assertEqual(source.peek().products[0].name, "Новый ответ");
  }],

  ["catalog-source: backend выключен — ошибка network, без кэша; следующий вызов снова делает GET", async () => {
    let fail = true;
    const { fetch, source } = setup(() => (fail ? networkFailure() : jsonResponse(200, catalogBody([testProduct()]))));
    const error = await rejectsWith(source.load());
    assertEqual(error.code, "network");
    assertEqual(error.retryable, true);
    assertEqual(source.peek(), null);
    fail = false;
    assertEqual((await source.load({ force: true })).total, 1);
    assertEqual(fetch.calls.map((call) => call.method), ["GET", "GET"]);
  }],

  ["catalog-source: ответ 500 — ошибка server; неверный формат — invalid_response", async () => {
    const server = setup(() => jsonResponse(500, { detail: "Internal Server Error" }));
    assertEqual((await rejectsWith(server.source.load())).code, "server");

    const { available, ...broken } = testProduct();
    const invalid = setup(() => jsonResponse(200, { products: [broken], total: 1 }));
    assertEqual((await rejectsWith(invalid.source.load())).code, "invalid_response");
    assertEqual(invalid.source.peek(), null, "частичные данные не сохраняются");
    assert(available === true);
  }],

  ["catalog-source: нужен api.getProductsV2", () => {
    let error = null;
    try {
      createCatalogSource({ api: {} });
    } catch (caught) {
      error = caught;
    }
    assert(error instanceof TypeError);
  }],
];
