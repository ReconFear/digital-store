// Тесты js/catalog/product-card.js: разметка прототипа, только текст, цена через formatMoney,
// наличие по available, без изображений и описаний, кнопка «Купить» неактивна.

import { createDom } from "../../storefront/js/core/dom.js";
import { formatMoney } from "../../storefront/js/core/api.js";
import { createPlaceholderCard, createProductCard } from "../../storefront/js/catalog/product-card.js";
import { BRAND } from "../../storefront/brand/brand.js";
import { allElements, byClass, createFakeDocument } from "./fake-dom.js";
import { testProduct } from "./fake-api.js";
import { assert, assertEqual } from "./assert.js";

const LABELS = BRAND.productTypeLabels;
const card = (product, extra = {}) =>
  createProductCard(createDom(createFakeDocument()), { product, labels: LABELS, ...extra });
const one = (root, name) => byClass(root, name)[0];
const tag = (root, name) => allElements(root).filter((node) => node.localName === name);

export const tests = [
  ["product-card: разметка и классы карточки прототипа", () => {
    const node = card(testProduct({ id: 12, name: "Тестовый ключ", product_type: "digital_key" }));
    assertEqual(node.localName, "article");
    assertEqual(node.className, "product-card");
    assertEqual(node.getAttribute("data-product-id"), "12");
    for (const name of ["product-image", "product-info", "product-category", "product-bottom", "product-price", "product-stock"]) {
      assertEqual(byClass(node, name).length, 1, name);
    }
    assertEqual(tag(node, "h3")[0].textContent, "Тестовый ключ");
    assertEqual(one(node, "product-category").textContent, "Ключи");
  }],

["product-card: заглушка изображения и безопасная ссылка на товар", () => {
  const product = testProduct({ name: "Тестовый файл" });
  const node = card(product);
  const image = one(node, "product-image");

  assertEqual(image.getAttribute("aria-hidden"), "true");
  assertEqual(image.textContent, "Тестовый файл");
  assertEqual(tag(node, "img").length, 0);

  const links = tag(node, "a");
  assertEqual(links.length, 1);
  assertEqual(links[0].getAttribute("href"), `#/product/${product.id}`);
  assertEqual(links[0].textContent, product.name);

  assert(allElements(node).every((el) => !el.hasAttribute("src")));
  assertEqual(byClass(node, "product-description").length, 0);
}],

 ["product-card: название с HTML остаётся текстом", () => {
  const evil = "<img src=x onerror=alert(1)>";
  const node = card(testProduct({ name: evil }));
  const title = tag(node, "h3")[0];
  const link = tag(node, "a")[0];

  assertEqual(title.childNodes.length, 1);
  assert(title.childNodes[0] === link);
  assertEqual(link.childNodes.length, 1);
  assertEqual(link.childNodes[0].nodeType, 3);
  assertEqual(link.textContent, evil);
  assertEqual(tag(node, "img").length, 0);
}],

  ["product-card: цена — через formatMoney клиента", () => {
    const price = { amount_minor: 99950, currency: "RUB", exponent: 2 };
    const node = card(testProduct({ price }));
    const text = one(node, "product-price").textContent;
    assertEqual(text, formatMoney(price));
    assert(text.includes("999,50") && text.includes("₽"), text);
    const whole = card(testProduct({ price: { amount_minor: 129000, currency: "RUB", exponent: 2 } }));
    assertEqual(one(whole, "product-price").textContent, `1 290 ₽`);
  }],

  ["product-card: непоказываемая валюта — честная подпись, без исключения", () => {
    const node = card(testProduct({ price: { amount_minor: 100, currency: "НЕ ВАЛЮТА", exponent: 2 } }));
    assertEqual(one(node, "product-price").textContent, "Цена недоступна");
  }],

  ["product-card: наличие, остаток и «Нет в наличии»", () => {
    const tracked = card(testProduct({ stock_quantity: 14 }));
    assertEqual(one(tracked, "product-stock").textContent, "В наличии · 14 шт.");
    assertEqual(one(tracked, "product-stock").className, "product-stock");

    const unlimited = card(testProduct({ inventory_mode: "unlimited", stock_quantity: null }));
    assertEqual(one(unlimited, "product-stock").textContent, "В наличии");

    const sold = card(testProduct({ available: false, stock_quantity: 0 }));
    assertEqual(one(sold, "product-stock").textContent, "Нет в наличии");
    assertEqual(one(sold, "product-stock").className, "product-stock unavailable");
  }],

  ["product-card: «Купить» видна, но неактивна и объяснена", () => {
    const node = card(testProduct(), { buyHintId: "hint-1" });
    const button = tag(node, "button")[0];
    assertEqual(button.className, "buy-button");
    assertEqual(one(button, "button-label").textContent, "Купить");
    assert(button.hasAttribute("disabled"));
    assertEqual(button.getAttribute("aria-describedby"), "hint-1");
    assertEqual(button.getAttribute("type"), "button");
  }],

  ["product-card: неизвестный тип — подпись «Другое»", () => {
    const node = card(testProduct({ product_type: "new_kind" }));
    assertEqual(one(node, "product-category").textContent, "Другое");
  }],

  ["product-card: карточка-скелетон T1 скрыта от читалок", () => {
    const node = createPlaceholderCard(createDom(createFakeDocument()));
    assertEqual(node.className, "product-card product-card--placeholder");
    assertEqual(node.getAttribute("aria-hidden"), "true");
    assertEqual(byClass(node, "skeleton").length, 5);
  }],
];
