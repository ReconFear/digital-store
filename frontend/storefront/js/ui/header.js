// Шапка Digital Store: логотип, поиск, навигация. Разметка и классы — из прототипа.
// Кнопка профиля скрыта, пока аккаунты выключены.
// T3: поиск по названию товара работает по уже загруженному каталогу (без запросов к API):
// ввод передаётся в onInput с небольшой задержкой, Enter — сразу в onInput и onSubmit.
// Если features.search выключен, поле неактивно, как в T1.

import { hrefFor } from "../core/router.js";

const NAV_ITEMS = [
  { route: "catalog", label: "Каталог" },
  { route: "cart", label: "Корзина" },
  { route: "order", label: "Мой заказ" },
];

// Экран товара относится к разделу «Каталог».
const SECTION_OF = { catalog: "catalog", product: "catalog", cart: "cart", order: "order" };

export const SEARCH_MAX_LENGTH = 100;
export const SEARCH_DEBOUNCE_MS = 150;

/**
 * @param {object} options
 * @param {object} [options.search] { onInput(query), onSubmit(query), debounceMs, schedule, cancel }
 */
export function createHeader({ dom, brand, config, search: searchOptions = {} }) {
  const { el } = dom;
  const {
    onInput = () => {},
    onSubmit = () => {},
    debounceMs = SEARCH_DEBOUNCE_MS,
    schedule = (fn, ms) => setTimeout(fn, ms),
    cancel = (id) => clearTimeout(id),
  } = searchOptions;

  const searchEnabled = config.features.search === true;
  let pending = null;
  const searchInput = el("input", {
    attrs: {
      id: "site-search",
      type: "search",
      name: "q",
      placeholder: searchEnabled ? "Поиск товаров..." : "Поиск товаров — скоро",
      autocomplete: "off",
      maxlength: String(SEARCH_MAX_LENGTH),
      disabled: !searchEnabled,
      "aria-describedby": searchEnabled ? null : "site-search-hint",
    },
  });

  function currentQuery() {
    return String(searchInput.value ?? "").slice(0, SEARCH_MAX_LENGTH);
  }

  function flush() {
    if (pending !== null) cancel(pending);
    pending = null;
    onInput(currentQuery());
  }

  function submitSearch(event) {
    event.preventDefault();
    if (!searchEnabled) return;
    flush();
    onSubmit(currentQuery());
  }

  if (searchEnabled) {
    searchInput.addEventListener("input", () => {
      if (pending !== null) cancel(pending);
      pending = schedule(flush, debounceMs);
    });
  }

  const search = el(
    "form",
    {
      className: "search",
      attrs: { role: "search" },
      listeners: { submit: submitSearch },
    },
    el("label", { className: "visually-hidden", text: "Поиск по каталогу", attrs: { for: "site-search" } }),
    searchInput,
    searchEnabled
      ? null
      : el("span", {
          className: "visually-hidden",
          text: "Поиск появится после подключения каталога.",
          attrs: { id: "site-search-hint" },
        }),
  );

  const links = new Map();
  const nav = el(
    "nav",
    { className: "site-nav", attrs: { "aria-label": "Основная навигация" } },
    NAV_ITEMS.map((item) => {
      const link = el("a", { className: "nav-link", text: item.label, attrs: { href: hrefFor(item.route) } });
      links.set(item.route, link);
      return link;
    }),
  );

  const node = el(
    "div",
    { className: "header-container" },
    el("a", { className: "logo", text: brand.name, attrs: { href: hrefFor("home") } }),
    search,
    nav,
  );

  function setActive(routeName) {
    const section = SECTION_OF[routeName];
    for (const [route, link] of links) {
      if (route === section) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    }
  }

  /** Синхронизировать поле с общим состоянием (например, после «Сбросить фильтры»). */
  function setSearchValue(value) {
    const text = String(value ?? "");
    if (pending === null && searchInput.value !== text) searchInput.value = text;
  }

  return Object.freeze({ node, setActive, setSearchValue });
}
