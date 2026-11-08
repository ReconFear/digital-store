// Тесты js/catalog/catalog-view.js и экранов главной/каталога: загрузка, пустой каталог,
// ошибка сети и «Повторить» (только GET), поиск/фильтры/сортировка без запросов,
// «Ничего не найдено», отмена при уходе со страницы, общий кэш и общие фильтры.

import { createDom } from "../../storefront/js/core/dom.js";
import { createStore } from "../../storefront/js/core/store.js";
import { createStoreApi } from "../../storefront/js/core/api.js";
import { createCatalogSource } from "../../storefront/js/catalog/source.js";
import { createCatalogView } from "../../storefront/js/catalog/catalog-view.js";
import { DEFAULT_FILTERS } from "../../storefront/js/catalog/model.js";
import { createScreens } from "../../storefront/js/pages/stubs.js";
import { BRAND } from "../../storefront/brand/brand.js";
import { allElements, byClass, createFakeDocument } from "./fake-dom.js";
import {
  catalogBody, deferred, fakeFetch, flush, jsonResponse, networkFailure, testProduct,
} from "./fake-api.js";
import { assert, assertEqual } from "./assert.js";

const CONFIG = Object.freeze({ apiBaseUrl: "http://127.0.0.1:8000", requestTimeoutMs: 10000 });
const rub = (amount_minor) => ({ amount_minor, currency: "RUB", exponent: 2 });

// Только тестовые данные.
const PRODUCTS = Object.freeze([
  testProduct({ id: 1, name: "Тестовый ключ Бета", product_type: "digital_key", price: rub(50000), stock_quantity: 2 }),
  testProduct({ id: 2, name: "Услуга тестовая", product_type: "service", inventory_mode: "unlimited", stock_quantity: null, price: rub(20000) }),
  testProduct({ id: 3, name: "Тестовый ключ Альфа", product_type: "digital_key", price: rub(30000), stock_quantity: 0, available: false }),
]);

function setup(respond, { filters = createStore({ ...DEFAULT_FILTERS }), signal, headingLevel = "h1" } = {}) {
  const fetch = fakeFetch(respond);
  const source = createCatalogSource({ api: createStoreApi(CONFIG, { fetch }) });
  const make = (options = {}) => createCatalogView({
    dom: createDom(createFakeDocument()),
    brand: BRAND,
    source,
    filters,
    headingLevel,
    signal,
    ...options,
  });
  return { fetch, source, filters, make, view: make() };
}

const ok = (products = PRODUCTS) => () => jsonResponse(200, catalogBody(products));
const cards = (root) => byClass(root, "product-card").filter((node) => !node.className.includes("placeholder"));
const cardIds = (root) => cards(root).map((node) => Number(node.getAttribute("data-product-id")));
const one = (root, name) => byClass(root, name)[0];
const chipLabels = (root) => byClass(root, "category").map((node) => node.textContent);
const click = (node) => node.dispatch("click", {});
const methods = (fetch) => fetch.calls.map((call) => call.method);

async function settle() {
  await flush();
  await flush();
}

export const tests = [
  ["catalog-view: загрузка — скелетоны, aria-busy и статус", async () => {
    const gate = deferred();
    const { view } = setup(() => gate.promise);
    const results = one(view.node, "catalog-results");
    assertEqual(results.getAttribute("aria-busy"), "true");
    assertEqual(byClass(view.node, "product-card--placeholder").length, 8);
    assertEqual(one(view.node, "catalog-count").textContent, "Загружаем каталог…");
    assertEqual(one(view.node, "catalog-count").getAttribute("aria-live"), "polite");
    gate.resolve(jsonResponse(200, catalogBody(PRODUCTS)));
    await view.ready;
    assertEqual(results.hasAttribute("aria-busy"), false);
    assertEqual(byClass(view.node, "product-card--placeholder").length, 0);
  }],

  ["catalog-view: товары в порядке API, чипы только по типам из ответа, счётчик", async () => {
    const { view } = setup(ok());
    await view.ready;
    assertEqual(cardIds(view.node), [1, 2, 3]);
    assertEqual(chipLabels(view.node), ["Все", "Ключи", "Услуги"]);
    assertEqual(one(view.node, "category").getAttribute("aria-pressed"), "true");
    assertEqual(one(view.node, "catalog-count").textContent, "Найдено: 3 товара");
    assertEqual(one(view.node, "notice").hasAttribute("hidden"), false, "пояснение о покупке видно");
    const sold = cards(view.node)[2];
    assertEqual(one(sold, "product-stock").textContent, "Нет в наличии", "недоступный товар остаётся на своём месте");
  }],

  ["catalog-view: поиск, тип, наличие и сортировка — без новых запросов", async () => {
    const { view, filters, fetch } = setup(ok());
    await view.ready;

    filters.set({ query: "ключ" });
    assertEqual(cardIds(view.node), [1, 3]);

    click(byClass(view.node, "category").find((chip) => chip.textContent === "Услуги"));
    assertEqual(filters.get().type, "service");
    assertEqual(cardIds(view.node), [], "запрос «ключ» + тип «Услуги»");
    filters.set({ query: "" });
    assertEqual(cardIds(view.node), [2]);
    const chips = byClass(view.node, "category");
    assertEqual(chips.map((chip) => chip.getAttribute("aria-pressed")), ["false", "false", "true"]);
    assertEqual(chips[2].className, "category active");

    click(chips[0]);
    const toggle = one(view.node, "filter-button");
    click(toggle);
    assertEqual(toggle.getAttribute("aria-pressed"), "true");
    assertEqual(cardIds(view.node), [1, 2], "только available: true");
    click(toggle);
    assertEqual(toggle.getAttribute("aria-pressed"), "false");

    const select = allElements(view.node).find((node) => node.localName === "select");
    select.value = "price_asc";
    select.dispatch("change", {});
    assertEqual(cardIds(view.node), [2, 3, 1]);
    select.value = "name_asc";
    select.dispatch("change", {});
    assertEqual(cardIds(view.node), [3, 1, 2]);

    filters.set({ query: "услуга" });
    await settle();
    assertEqual(fetch.calls.length, 1, "фильтры и сортировка не обращаются к API");
  }],

  ["catalog-view: «Ничего не найдено» и «Сбросить фильтры»", async () => {
    const { view, filters } = setup(ok());
    await view.ready;
    filters.set({ query: "такого товара нет", inStockOnly: true, sort: "price_desc" });
    assertEqual(cards(view.node).length, 0);
    assertEqual(one(view.node, "state-title").textContent, "Ничего не найдено");
    assertEqual(one(view.node, "catalog-count").textContent, "Найдено: 0 товаров");
    const reset = byClass(one(view.node, "catalog-results"), "button-secondary")[0];
    assertEqual(reset.textContent.includes("Сбросить фильтры"), true);
    click(reset);
    assertEqual(filters.get(), { ...DEFAULT_FILTERS });
    assertEqual(cardIds(view.node), [1, 2, 3]);
  }],

  ["catalog-view: пустой каталог — честное сообщение, без товаров-заглушек", async () => {
    const { view } = setup(ok([]));
    await view.ready;
    assertEqual(cards(view.node).length, 0);
    assertEqual(byClass(view.node, "product-card--placeholder").length, 0);
    assertEqual(one(view.node, "state-title").textContent, "В каталоге пока нет товаров");
    assertEqual(one(view.node, "notice").hasAttribute("hidden"), true);
    assertEqual(chipLabels(view.node), ["Все"]);
  }],

  ["catalog-view: backend выключен — ошибка подключения и «Повторить» делает только GET", async () => {
    let backendUp = false;
    const { view, fetch, filters } = setup(() => (backendUp ? jsonResponse(200, catalogBody(PRODUCTS)) : networkFailure()));
    await view.ready;
    const results = one(view.node, "catalog-results");
    const alert = one(results, "message--error");
    assert(alert, "сообщение об ошибке подключения");
    assertEqual(alert.getAttribute("role"), "alert");
    assertEqual(one(alert, "message-title").textContent, "Нет связи с магазином");
    assertEqual(one(alert, "message-text").textContent,
      "Не удалось связаться с сервером магазина. Проверьте подключение и попробуйте ещё раз.");
    assertEqual(cards(view.node).length, 0, "никаких демонстрационных товаров");
    assertEqual(byClass(view.node, "product-card--placeholder").length, 0, "скелетоны убраны");
    assertEqual(one(view.node, "notice").hasAttribute("hidden"), true);
    filters.set({ query: "ключ", inStockOnly: true });
    assert(one(one(view.node, "catalog-results"), "message--error"), "смена фильтров не скрывает ошибку");
    assertEqual(byClass(view.node, "state-title").length, 0, "ни «пустого каталога», ни «ничего не найдено»");
    filters.set({ ...DEFAULT_FILTERS });

    const retry = byClass(alert, "button-secondary")[0];
    assert(retry.textContent.includes("Повторить"));
    click(retry);
    await settle();
    assertEqual(cards(view.node).length, 0, "backend всё ещё выключен");
    assert(one(one(view.node, "catalog-results"), "message--error"), "ошибка показана снова");

    backendUp = true;
    click(byClass(one(view.node, "catalog-results"), "button-secondary")[0]);
    await settle();
    assertEqual(cardIds(view.node), [1, 2, 3]);
    assertEqual(methods(fetch), ["GET", "GET", "GET"]);
    assert(fetch.calls.every((call) => call.url === "http://127.0.0.1:8000/api/v2/products" && call.body === undefined));
  }],

  ["catalog-view: пока «Повторить» выполняется, кнопка занята и повторно не нажимается", async () => {
    const gate = deferred();
    let attempt = 0;
    const { view, fetch } = setup(() => (++attempt === 1 ? networkFailure() : gate.promise));
    await view.ready;
    const retry = byClass(one(view.node, "catalog-results"), "button-secondary")[0];
    click(retry);
    click(retry);
    await flush();
    assertEqual(retry.getAttribute("aria-busy"), "true");
    assertEqual(fetch.calls.length, 2, "двойное нажатие — один повтор");
    gate.resolve(jsonResponse(200, catalogBody(PRODUCTS)));
    await settle();
    assertEqual(cards(view.node).length, 3);
  }],

  ["catalog-view: ошибка сервера и неверный ответ — понятный текст и «Повторить»", async () => {
    const server = setup(() => jsonResponse(503, { detail: "Service Unavailable" }));
    await server.view.ready;
    const state = one(server.view.node, "state--error");
    assertEqual(one(state, "state-title").textContent, "Не удалось загрузить каталог");
    assert(state.textContent.includes("На сервере магазина произошла ошибка"));
    assert(byClass(state, "button-secondary").length === 1);

    const invalid = setup(() => jsonResponse(200, { products: [{ id: 1 }], total: 1 }));
    await invalid.view.ready;
    assert(one(invalid.view.node, "state--error").textContent.includes("неожиданный ответ"));
    assertEqual(cards(invalid.view.node).length, 0, "частичные данные не показываются");
  }],

  ["catalog-view: уход со страницы — поздний ответ не показывается, фильтры отписаны", async () => {
    const gate = deferred();
    const controller = new AbortController();
    const filters = createStore({ ...DEFAULT_FILTERS });
    const { view, fetch } = setup(() => gate.promise, { filters, signal: controller.signal });
    await flush();
    controller.abort();
    gate.resolve(jsonResponse(200, catalogBody(PRODUCTS)));
    await settle();
    assertEqual(cards(view.node).length, 0, "ушедший экран не обновляется");
    assertEqual(fetch.calls[0].signal.aborted, true, "ненужный запрос отменён");
    filters.set({ query: "ключ" });
    assertEqual(cards(view.node).length, 0);
  }],

  ["catalog-view: ответ пришёл, но экран уже закрыт — ничего не рисуется", async () => {
    const gate = deferred();
    const controller = new AbortController();
    // Источник-заглушка игнорирует отмену: так проверяется защита самого экрана.
    const source = { peek: () => null, load: () => gate.promise };
    const view = createCatalogView({
      dom: createDom(createFakeDocument()),
      brand: BRAND,
      source,
      filters: createStore({ ...DEFAULT_FILTERS }),
      signal: controller.signal,
    });
    controller.abort();
    gate.resolve({ products: PRODUCTS, total: PRODUCTS.length });
    await view.ready;
    assertEqual(cards(view.node).length, 0);
    assertEqual(chipLabels(view.node), ["Все"]);
  }],

  ["catalog-view: главная и каталог — общий кэш и общие фильтры", async () => {
    const filters = createStore({ ...DEFAULT_FILTERS });
    const { fetch, make, view } = setup(ok(), { filters });
    await view.ready;
    filters.set({ query: "услуга" });
    const second = make({ headingLevel: "h2", skeletonCards: 3 });
    assertEqual(byClass(second.node, "product-card--placeholder").length, 0, "из кэша — без мигания загрузки");
    assertEqual(cardIds(second.node), [2], "фильтры сохранились");
    assertEqual(fetch.calls.length, 1);
  }],

  ["catalog-view: выбранного типа нет в новых данных — показываются все", async () => {
    const filters = createStore({ ...DEFAULT_FILTERS, type: "physical" });
    const { view } = setup(ok(), { filters });
    await view.ready;
    assertEqual(filters.get().type, "all");
    assertEqual(cardIds(view.node), [1, 2, 3]);
  }],

  ["catalog-view: экраны — главная с hero и h2, каталог с h1; без каталога — макеты T1", async () => {
    const fetch = fakeFetch(ok());
    const catalog = {
      source: createCatalogSource({ api: createStoreApi(CONFIG, { fetch }) }),
      filters: createStore({ ...DEFAULT_FILTERS }),
    };
    const screens = createScreens({ dom: createDom(createFakeDocument()), brand: BRAND, catalog });
    const home = screens.home({}, { signal: new AbortController().signal });
    assertEqual(byClass(home.node, "hero").length, 1);
    assertEqual(allElements(home.node).filter((n) => n.localName === "h2")[0].textContent, "Каталог");
    assertEqual(byClass(home.node, "product-card--placeholder").length, 3);
    const page = screens.catalog({}, { signal: new AbortController().signal });
    assertEqual(byClass(page.node, "hero").length, 0);
    assertEqual(page.title, "Каталог");
    assertEqual(allElements(page.node).filter((n) => n.localName === "h1")[0].textContent, "Каталог");
    await settle();
    assertEqual(fetch.calls.length, 1, "оба экрана ждали один запрос");
    assertEqual(cards(page.node).length, 3);

    const offline = createScreens({ dom: createDom(createFakeDocument()), brand: BRAND });
    const placeholder = offline.catalog();
    assertEqual(byClass(placeholder.node, "product-card--placeholder").length, 8);
    assertEqual(byClass(placeholder.node, "notice").length, 1);
  }],
];
