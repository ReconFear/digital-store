// Экраны витрины в дизайне Digital Store.
// T3: главная (hero + каталог) и каталог показывают настоящие товары из GET /api/v2/products
// (js/catalog/catalog-view.js). Товар, корзина и заказ пока остаются заглушками.
// Если каталог не передан (features.catalog выключен), показываются макеты карточек T1.
import { hrefFor } from "../core/router.js";
import { createButton } from "../ui/button.js";
import { stockInfo } from "../catalog/model.js";
import { createStates } from "../ui/states.js";
import { createCatalogView } from "../catalog/catalog-view.js";
import { createPlaceholderCard, priceText } from "../catalog/product-card.js";

// На главной — три карточки, как в согласованном прототипе; в каталоге — полная сетка.
const PLACEHOLDER_CARDS = { home: 3, catalog: 8 };
const PLACEHOLDER_CHIPS = 3;

/**
 * @param {object} options
 * @param {object} options.dom
 * @param {object} options.brand
 * @param {{ source: object, filters: object } | null} [options.catalog] источник каталога и общие фильтры
 */
export function createScreens({ dom, brand, catalog = null, api = null, cartStore = null, onAdd = null }) {
  const { el } = dom;
  const states = createStates(dom);

  function skeleton(modifier) {
    return el("span", { className: `skeleton skeleton--${modifier}` });
  }

  /** Каталог выключен: макет раздела без данных (T1). */
  function placeholderSection({ headingLevel, cards }) {
    const heading = el(headingLevel, { text: "Каталог", attrs: { id: "catalog-title" } });
    const chips = [
      el("button", { className: "category active", text: "Все", attrs: { type: "button", disabled: true } }),
      ...Array.from({ length: PLACEHOLDER_CHIPS }, () =>
        el("span", { className: "category", attrs: { "aria-hidden": "true" } }, skeleton("stock"))),
    ];
    return el(
      "section",
      { className: "catalog", attrs: { "aria-labelledby": "catalog-title" } },
      el(
        "div",
        { className: "catalog-header" },
        heading,
        el("button", {
          className: "filter-button",
          text: "Только в наличии",
          attrs: { type: "button", disabled: true },
        }),
      ),
      el("div", { className: "categories", attrs: { role: "group", "aria-label": "Типы товаров" } }, chips),
      states.notice({
        text: "Товары появятся здесь после подключения каталога магазина. Сейчас показаны макеты карточек — купить их нельзя.",
      }),
      el(
        "div",
        { className: "products-grid" },
        Array.from({ length: cards }, () => createPlaceholderCard(dom)),
      ),
    );
  }

  function catalogSection({ headingLevel, cards, signal }) {
    if (!catalog) return placeholderSection({ headingLevel, cards });
    return createCatalogView({
      dom,
      brand,
      source: catalog.source,
      filters: catalog.filters,
      headingLevel,
      skeletonCards: cards,
      signal,
      onAdd,
    }).node;
  }

  function home(params = {}, { signal } = {}) {
    const hero = el(
      "section",
      { className: "hero", attrs: { "aria-labelledby": "hero-title" } },
      el("p", { className: "hero-label", text: brand.hero.label }),
      el("h1", { text: brand.hero.title, attrs: { id: "hero-title" } }),
      el("p", { className: "hero-text", text: brand.hero.text }),
    );
    return { title: null, node: el("div", { className: "page page--home" }, hero, catalogSection({ headingLevel: "h2", cards: PLACEHOLDER_CARDS.home, signal })) };
  }

  function catalogScreen(params = {}, { signal } = {}) {
    return { title: "Каталог", node: el("div", { className: "page page--catalog" }, catalogSection({ headingLevel: "h1", cards: PLACEHOLDER_CARDS.catalog, signal })) };
  }

  function product({ id }, { signal } = {}) {
    const details = el("div", {
  className: "product-details",
  attrs: { role: "status" },
  text: "Загружаем товар…",
});
if (api?.getProductV2) {
  api.getProductV2(id, { signal })
    .then((item) => {
      if (signal?.aborted) return;

     const stock = stockInfo(item);

      details.replaceChildren(
  el("h2", { text: item.name }),
  el("p", {
    className: "product-price",
    text: priceText(item.price),
  }),
  el("p", {
    className: stock.available ? "product-stock" : "product-stock unavailable",
    text: stock.text,
  }),
  createButton(dom, {
    label: "В корзину",
    disabled: !stock.available || typeof onAdd !== "function",
    onClick: () => onAdd?.(item.id),
  }).node,
);
    })
    .catch(() => {
      if (signal?.aborted) return;

      details.textContent = "Не удалось загрузить товар.";
    });
} else {
  details.textContent = "Данные товара недоступны.";
}
    return {
      title: "Товар",
      node: el(
        "div",
        { className: "page page--product" },
        el("a", { className: "back-link", text: "← К каталогу", attrs: { href: hrefFor("catalog") } }),
        el("h1", { className: "page-title", text: "Товар" }),
        el(
          "div",
          { className: "product-layout" },
          el("div", { className: "product-image", attrs: { "aria-hidden": "true" } }),
          el(
            "div",
            { className: "product-panel" },
            details,
            ),
        ),
      ),
    };
  }

    function cart(params = {}, { signal } = {}) {
    const content = el("div", { className: "cart-content" });
    const productsById = new Map();

    function cartButton(text, label, action, disabled = false) {
      return el("button", {
        className: "button-secondary",
        text,
        attrs: {
          type: "button",
          "aria-label": label,
          ...(disabled ? { disabled: true } : {}),
        },
        listeners: {
          click: () => {
            action();
            renderCart();
          },
        },
      });
    }

    function renderCart() {
      const items = cartStore?.get().items ?? [];

      if (items.length === 0) {
        content.replaceChildren(states.empty({
          title: "Корзина пуста",
          text: "Ты пока не добавил ни одного товара.",
          actions: [
            { label: "К каталогу", href: hrefFor("catalog") },
          ],
        }));
        return;
      }

      content.replaceChildren(
        el(
          "ul",
          { className: "cart-items" },
          items.map((item) =>
            el(
              "li",
              { className: "cart-item" },

              el("span", {
               text: `${productsById.get(item.product_id)?.name ?? `Товар №${item.product_id}`} — ${item.quantity} шт.`,
              }),
              el("span", {
              text: productsById.has(item.product_id)
              ? ` · ${priceText(productsById.get(item.product_id).price)} / шт.`
             : " · Цена недоступна",
              }),
              el(
                "div",
                { className: "state-actions" },

                cartButton(
                  "−",
                  "Уменьшить количество",
                  () => cartStore.setQuantity(item.product_id, item.quantity - 1),
                  item.quantity <= 1,
                ),

                cartButton(
                  "+",
                  "Увеличить количество",
                  () => cartStore.add(item.product_id),
                  item.quantity >= 1000,
                ),

                cartButton(
                  "Удалить",
                  "Удалить товар",
                  () => cartStore.remove(item.product_id),
                ),
              ),
            ),
          ),
        ),
      );
    const firstPrice = productsById.get(items[0].product_id)?.price;

const pricesAvailable = firstPrice && items.every((item) => {
  const price = productsById.get(item.product_id)?.price;

  return price &&
    price.currency === firstPrice.currency &&
    price.exponent === firstPrice.exponent &&
    Number.isSafeInteger(price.amount_minor);
});

const totalMinor = pricesAvailable
  ? items.reduce(
      (sum, item) =>
        sum + productsById.get(item.product_id).price.amount_minor * item.quantity,
      0
    )
  : null;

const totalText = totalMinor !== null && Number.isSafeInteger(totalMinor)
  ? priceText({ ...firstPrice, amount_minor: totalMinor })
  : "Сумма уточняется";

content.append(
  el("p", {
    className: "cart-total",
    text: `Итого: ${totalText}`,
  })
);
    }

    renderCart();
    if (catalog?.source) {
  catalog.source.load({ signal })
    .then(({ products }) => {
      if (signal?.aborted) return;

      for (const product of products) {
        productsById.set(product.id, product);
      }

      renderCart();
    })
    .catch(() => {
      // Даже без связи с API корзина остаётся доступной.
    });
}

    return {
      title: "Корзина",
      node: el(
        "div",
        { className: "page page--cart" },

        el("h1", {
          className: "page-title",
          text: "Корзина",
        }),

        content,

        states.notice({
          text: "Оформление заказов и оплата пока отключены.",
        }),
      ),
    };
  }

  function order() {
    return {
      title: "Мой заказ",
      node: el(
        "div",
        { className: "page page--order" },
        el("h1", { className: "page-title", text: "Мой заказ" }),
        states.notice({
          text: "Здесь будут статус оплаты и получение цифровых товаров. Оформление заказов пока недоступно.",
        }),
        states.empty({ actions: [{ label: "К каталогу", href: hrefFor("catalog") }] }),
      ),
    };
  }

  function notFound() {
    return {
      title: "Страница не найдена",
      node: el(
        "div",
        { className: "page page--not-found" },
        el("h1", { className: "page-title", text: "Страница не найдена" }),
        states.empty({
          text: "Такой страницы нет. Проверьте адрес или вернитесь в магазин.",
          actions: [
            { label: "На главную", href: hrefFor("home") },
            { label: "К каталогу", href: hrefFor("catalog") },
          ],
        }),
      ),
    };
  }

  return Object.freeze({ home, catalog: catalogScreen, product, cart, order, not_found: notFound });
}
