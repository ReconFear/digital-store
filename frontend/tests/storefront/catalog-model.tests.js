// Тесты js/catalog/model.js: поиск, фильтры, «Только в наличии», сортировка, подписи.

import {
  DEFAULT_FILTERS, MAX_QUERY_LENGTH, availableTypes, compareMoney, isDefaultFilters, normalizeQuery,
  plural, resultsText, selectProducts, sortProducts, stockInfo, typeGroup, typeLabel,
} from "../../storefront/js/catalog/model.js";
import { testProduct } from "./fake-api.js";
import { assert, assertEqual } from "./assert.js";

const rub = (amount_minor, exponent = 2) => ({ amount_minor, currency: "RUB", exponent });
const ids = (list) => list.map((product) => product.id);
const NBSP = " ";

// Только тестовые данные.
const LIST = Object.freeze([
  testProduct({ id: 1, name: "Тестовый ключ Альфа", product_type: "digital_key", price: rub(50000), stock_quantity: 3 }),
  testProduct({ id: 2, name: "Тестовый файл", product_type: "digital_file", inventory_mode: "unlimited", stock_quantity: null, price: rub(10000) }),
  testProduct({ id: 3, name: "Ёлочная услуга", product_type: "service", inventory_mode: "unlimited", stock_quantity: null, price: rub(10000) }),
  testProduct({ id: 4, name: "Тестовый ключ Бета", product_type: "digital_key", price: rub(25000), stock_quantity: 0, available: false }),
  testProduct({ id: 5, name: "Будущий тип", product_type: "new_kind", price: rub(99999) }),
]);

export const tests = [
  ["catalog-model: запрос нормализуется — регистр, «ё», пробелы, длина", () => {
    assertEqual(normalizeQuery("  КЛЮЧ   Ёлка "), "ключ елка");
    assertEqual(normalizeQuery(null), "");
    assertEqual(normalizeQuery("я".repeat(500)).length, MAX_QUERY_LENGTH);
  }],

  ["catalog-model: поиск по названию без учёта регистра и «ё»/«е»", () => {
    assertEqual(ids(selectProducts(LIST, { ...DEFAULT_FILTERS, query: "КЛЮЧ" })), [1, 4]);
    assertEqual(ids(selectProducts(LIST, { ...DEFAULT_FILTERS, query: "елочная" })), [3]);
    assertEqual(ids(selectProducts(LIST, { ...DEFAULT_FILTERS, query: "ёлочная" })), [3]);
    assertEqual(ids(selectProducts(LIST, { ...DEFAULT_FILTERS, query: "  альфа " })), [1]);
    assertEqual(ids(selectProducts(LIST, { ...DEFAULT_FILTERS, query: "нет такого" })), []);
  }],

  ["catalog-model: спецсимволы в запросе — обычный текст, не регулярное выражение", () => {
    for (const query of ["(", ".*", "[", "\\", "$^", "?"]) {
      assertEqual(ids(selectProducts(LIST, { ...DEFAULT_FILTERS, query })), [], `запрос ${query}`);
    }
    const tricky = [testProduct({ id: 9, name: "Ключ (DLC) .*" })];
    assertEqual(ids(selectProducts(tricky, { ...DEFAULT_FILTERS, query: "(dlc) .*" })), [9]);
  }],

  ["catalog-model: фильтр по product_type; неизвестный тип — группа «Другое»", () => {
    assertEqual(ids(selectProducts(LIST, { ...DEFAULT_FILTERS, type: "digital_key" })), [1, 4]);
    assertEqual(ids(selectProducts(LIST, { ...DEFAULT_FILTERS, type: "service" })), [3]);
    assertEqual(ids(selectProducts(LIST, { ...DEFAULT_FILTERS, type: "other" })), [5]);
    assertEqual(typeGroup("new_kind"), "other");
    assertEqual(typeLabel("new_kind", { other: "Другое" }), "Другое");
    assertEqual(typeLabel("digital_key", { digital_key: "Ключи" }), "Ключи");
  }],

  ["catalog-model: чипы — только типы из ответа, в постоянном порядке", () => {
    assertEqual(availableTypes(LIST), ["digital_key", "digital_file", "service", "other"]);
    assertEqual(availableTypes([]), []);
  }],

  ["catalog-model: «Только в наличии» — строго по полю available", () => {
    const list = [
      testProduct({ id: 1, available: true, stock_quantity: 1 }),
      testProduct({ id: 2, available: false, stock_quantity: 7 }),
      testProduct({ id: 3, inventory_mode: "unlimited", stock_quantity: null, available: true }),
    ];
    assertEqual(ids(selectProducts(list, { ...DEFAULT_FILTERS, inStockOnly: true })), [1, 3]);
    assertEqual(ids(selectProducts(list, DEFAULT_FILTERS)), [1, 2, 3], "без фильтра видны все, включая недоступные");
  }],

  ["catalog-model: сортировка по цене — при равенстве по id; наличие не влияет", () => {
    assertEqual(ids(sortProducts(LIST, "price_asc")), [2, 3, 4, 1, 5]);
    assertEqual(ids(sortProducts(LIST, "price_desc")), [5, 1, 4, 2, 3]);
    const lengths = [
      testProduct({ id: 1, price: rub(100000) }),
      testProduct({ id: 2, price: rub(9000) }),
      testProduct({ id: 3, price: rub(25000) }),
    ];
    assertEqual(ids(sortProducts(lengths, "price_asc")), [2, 3, 1], "90 < 250 < 1000 — числа, не строки");
  }],

  ["catalog-model: сортировка по названию — по-русски, числа по значению", () => {
    const list = [
      testProduct({ id: 1, name: "Ключ 10" }),
      testProduct({ id: 2, name: "яблоко" }),
      testProduct({ id: 3, name: "Ключ 2" }),
      testProduct({ id: 4, name: "Альфа" }),
    ];
    assertEqual(sortProducts(list, "name_asc").map((p) => p.name), ["Альфа", "Ключ 2", "Ключ 10", "яблоко"]);
  }],

  ["catalog-model: «По умолчанию» — порядок ответа API; исходный массив не меняется", () => {
    const list = [testProduct({ id: 3 }), testProduct({ id: 1 }), testProduct({ id: 2 })];
    const before = ids(list);
    assertEqual(ids(sortProducts(list, "default")), [3, 1, 2]);
    sortProducts(list, "price_desc");
    selectProducts(list, { ...DEFAULT_FILTERS, sort: "name_asc" });
    assertEqual(ids(list), before);
    assertEqual(ids(selectProducts(list, { ...DEFAULT_FILTERS, sort: "<script>" })), [3, 1, 2], "неизвестная сортировка = по умолчанию");
  }],

  ["catalog-model: цены сравниваются в целых копейках с учётом exponent", () => {
    assertEqual(compareMoney(rub(9999), rub(100, 0)), -1, "99,99 < 100");
    assertEqual(compareMoney(rub(10000), rub(100, 0)), 0, "100,00 = 100");
    assertEqual(compareMoney(rub(9007199254740991), rub(9007199254740990)), 1, "большие суммы без потери точности");
    assertEqual(compareMoney({ amount_minor: 1, currency: "EUR", exponent: 2 }, rub(1)), -1, "разные валюты — по коду");
  }],

  ["catalog-model: всё вместе — поиск, тип, наличие и сортировка", () => {
    const filters = { query: "тестовый", type: "digital_key", inStockOnly: true, sort: "price_asc" };
    assertEqual(ids(selectProducts(LIST, filters)), [1]);
    assertEqual(isDefaultFilters(DEFAULT_FILTERS), true);
    assertEqual(isDefaultFilters({ ...DEFAULT_FILTERS, query: "   " }), true);
    assertEqual(isDefaultFilters(filters), false);
  }],

  ["catalog-model: подпись наличия и остатка", () => {
    assertEqual(stockInfo(testProduct({ stock_quantity: 14 })), { available: true, text: "В наличии · 14 шт." });
    assertEqual(stockInfo(testProduct({ stock_quantity: 1500 })).text, `В наличии · 1${NBSP}500 шт.`);
    assertEqual(stockInfo(testProduct({ inventory_mode: "unlimited", stock_quantity: null })), { available: true, text: "В наличии" });
    assertEqual(stockInfo(testProduct({ available: false, stock_quantity: 0 })), { available: false, text: "Нет в наличии" });
    assertEqual(stockInfo(testProduct({ available: false, stock_quantity: 9 })).available, false, "решает available, не остаток");
  }],

  ["catalog-model: счётчик со склонением", () => {
    assertEqual(resultsText(1), "Найдено: 1 товар");
    assertEqual(resultsText(3), "Найдено: 3 товара");
    assertEqual(resultsText(5), "Найдено: 5 товаров");
    assertEqual(resultsText(11), "Найдено: 11 товаров");
    assertEqual(resultsText(21), "Найдено: 21 товар");
    assertEqual(resultsText(112), "Найдено: 112 товаров");
    assertEqual(resultsText(0), "Найдено: 0 товаров");
    assert(plural(22, ["a", "b", "c"]) === "b");
  }],
];
