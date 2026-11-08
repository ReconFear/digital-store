// Карточка товара — разметка и классы прототипа Digital Store (index.html, .product-card).
// Изображений и описаний в API нет: вместо фото — градиентная заглушка прототипа с названием.
// Все данные выводятся только текстом. Кнопка «Купить» видна, но неактивна до появления корзины.

import { createButton } from "../ui/button.js";
import { formatMoney } from "../core/api.js";
import { stockInfo, typeLabel } from "./model.js";
import { hrefFor } from "../core/router.js";
/** Цена через formatMoney клиента; если валюту не удаётся показать — честная подпись, без выдумки. */
export function priceText(price) {
  try {
    return formatMoney(price);
  } catch {
    return "Цена недоступна";
  }
}

/**
 * @param {object} dom результат createDom(document)
 * @param {object} options
 * @param {object} options.product товар API v2
 * @param {object} [options.labels] подписи типов (brand.productTypeLabels)
 * @param {string} [options.buyHintId] id пояснения, почему покупка недоступна
 */
export function createProductCard(dom, { product, labels = {}, buyHintId = null, onAdd = null }) {
  const { el } = dom;
  const stock = stockInfo(product);
const buy = createButton(dom, {
  label: onAdd ? "В корзину" : "Купить",
  disabled: !onAdd || !stock.available,
  onClick: onAdd ? () => onAdd(product.id) : undefined,
  attrs: onAdd ? {} : { "aria-describedby": buyHintId },
});
  return el(
    "article",
    { className: "product-card", attrs: { "data-product-id": String(product.id) } },
    el("div", { className: "product-image", attrs: { "aria-hidden": "true" } }, el("span", { text: product.name })),
    el(
      "div",
      { className: "product-info" },
      el("p", { className: "product-category", text: typeLabel(product.product_type, labels) }),
      el(
  "h3",
  {},
  el("a", {
    text: product.name,
    attrs: { href: hrefFor("product", { id: product.id }) },
  }),
),
      el(
        "div",
        { className: "product-bottom" },
        el(
          "div",
          {},
          el("p", { className: "product-price", text: priceText(product.price) }),
          el("p", { className: stock.available ? "product-stock" : "product-stock unavailable", text: stock.text }),
        ),
        buy.node,
      ),
    ),
  );
}

/** Макет карточки без данных (T1): скелетоны на время загрузки. Скрыт от экранных читалок. */
export function createPlaceholderCard(dom) {
  const { el } = dom;
  const skeleton = (modifier) => el("span", { className: `skeleton skeleton--${modifier}` });
  return el(
    "article",
    { className: "product-card product-card--placeholder", attrs: { "aria-hidden": "true" } },
    el("div", { className: "product-image" }),
    el(
      "div",
      { className: "product-info" },
      skeleton("category"),
      skeleton("title"),
      el(
        "div",
        { className: "product-bottom" },
        el("div", {}, skeleton("price"), skeleton("stock")),
        skeleton("button"),
      ),
    ),
  );
}
