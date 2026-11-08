// Экран каталога: заголовок, «Только в наличии», сортировка, чипы типов, счётчик, сетка карточек.
// Разметка и классы — из прототипа Digital Store (.catalog-header, .filter-button, .categories,
// .category, .products-grid). Состояния — компоненты T1/T2 (states.js, button.js).
//
// Данные берутся только из source.load() (GET /api/v2/products). Поиск, фильтры и сортировка
// считаются по загруженному списку и не делают сетевых запросов. При уходе со страницы
// (signal) экран отписывается от фильтров и не показывает поздние ответы.

import { createButton } from "../ui/button.js";
import { createStates } from "../ui/states.js";
import { createPlaceholderCard, createProductCard } from "./product-card.js";
import { DEFAULT_FILTERS, SORTS, availableTypes, resultsText, selectProducts, typeLabel } from "./model.js";
import { isAbortError } from "./source.js";

const NETWORK_CODES = new Set(["network", "timeout"]);
let viewCounter = 0;

/**
 * @param {object} options
 * @param {object} options.dom результат createDom(document)
 * @param {object} options.brand BRAND (подписи типов товаров)
 * @param {object} options.source createCatalogSource(...)
 * @param {object} options.filters createStore с полями DEFAULT_FILTERS (общий для главной и каталога)
 * @param {"h1"|"h2"} [options.headingLevel]
 * @param {number} [options.skeletonCards] сколько карточек-скелетонов показывать при загрузке
 * @param {AbortSignal} [options.signal] отменяется маршрутизатором при уходе со страницы
 */
export function createCatalogView({ dom, brand, source, filters, headingLevel = "h1", skeletonCards = 8, signal, onAdd = null }) {
  const { el } = dom;
  const states = createStates(dom);
  const uid = `catalog-${++viewCounter}`;
  const labels = brand.productTypeLabels || {};

  let data = null;
  let destroyed = false;
  let loadSeq = 0;
  let chipNodes = [];
  const cards = new Map();

  const heading = el(headingLevel, { text: "Каталог", attrs: { id: `${uid}-title`, tabindex: "-1" } });

  const stockToggle = el("button", {
    className: "filter-button",
    text: "Только в наличии",
    attrs: { type: "button", "aria-pressed": "false" },
    listeners: { click: () => filters.set((state) => ({ inStockOnly: !state.inStockOnly })) },
  });

  const sortSelect = el(
    "select",
    {
      className: "sort-select",
      attrs: { id: `${uid}-sort`, name: "sort" },
      listeners: { change: () => filters.set({ sort: sortSelect.value }) },
    },
    SORTS.map((sort) => el("option", { text: sort.label, attrs: { value: sort.id } })),
  );

  const chips = el("div", { className: "categories", attrs: { role: "group", "aria-label": "Типы товаров" } });
  const count = el("p", { className: "catalog-count", attrs: { role: "status", "aria-live": "polite" } });
  const buyHintId = `${uid}-buy-hint`;
  const buyHint = el("p", {
    className: "visually-hidden",
    text: "Покупка пока недоступна: оформление заказов скоро появится.",
    attrs: { id: buyHintId },
  });
  const notice = states.notice({
    text: "Оформление покупки скоро появится. Сейчас можно посмотреть товары, цены и наличие.",
  });
  notice.setAttribute("hidden", "");
  const results = el("div", { className: "catalog-results" });

  const node = el(
    "section",
    { className: "catalog", attrs: { "aria-labelledby": `${uid}-title` } },
    el(
      "div",
      { className: "catalog-header" },
      heading,
      el(
        "div",
        { className: "catalog-controls" },
        stockToggle,
        el("label", { className: "visually-hidden", text: "Сортировка", attrs: { for: `${uid}-sort` } }),
        sortSelect,
      ),
    ),
    chips,
    count,
    notice,
    results,
    buyHint,
  );

  function setCount(text) {
    // Тот же текст не перезаписываем, чтобы экранная читалка не повторяла его.
    if (count.textContent !== text) count.textContent = text;
  }

  function chip(type, label) {
    return el("button", {
      className: "category",
      text: label,
      attrs: { type: "button", "data-type": type, "aria-pressed": "false" },
      listeners: { click: () => filters.set({ type }) },
    });
  }

  function renderChips() {
    const types = data ? availableTypes(data.products) : [];
    chipNodes = [chip("all", "Все"), ...types.map((type) => chip(type, typeLabel(type, labels)))];
    chips.replaceChildren(...chipNodes);
    syncControls(filters.get());
  }

  function syncControls(state) {
    stockToggle.setAttribute("aria-pressed", state.inStockOnly ? "true" : "false");
    if (sortSelect.value !== state.sort) sortSelect.value = state.sort;
    for (const chipNode of chipNodes) {
      const active = chipNode.getAttribute("data-type") === state.type;
      chipNode.className = active ? "category active" : "category";
      chipNode.setAttribute("aria-pressed", active ? "true" : "false");
    }
  }

  function cardFor(product) {
    let card = cards.get(product.id);
    if (!card) {
    card = createProductCard(dom, { product, labels, buyHintId, onAdd });
      cards.set(product.id, card);
    }
    return card;
  }

  function resetFilters() {
    filters.set({ ...DEFAULT_FILTERS });
    chipNodes[0]?.focus?.();
  }

  function nothingFound() {
    const reset = createButton(dom, { label: "Сбросить фильтры", variant: "secondary", onClick: resetFilters });
    return el(
      "div",
      { className: "state" },
      el("p", { className: "state-title", text: "Ничего не найдено" }),
      el("p", { text: "Попробуйте изменить запрос или сбросить фильтры." }),
      el("div", { className: "state-actions" }, reset.node),
    );
  }

  function renderResults() {
    const state = filters.get();
    syncControls(state);
    if (!data) return;

    if (data.products.length === 0) {
      notice.setAttribute("hidden", "");
      setCount("");
      results.replaceChildren(states.empty({
        title: "В каталоге пока нет товаров",
        text: "Загляните позже — товары появятся здесь.",
      }));
      return;
    }

    notice.removeAttribute("hidden");
    const list = selectProducts(data.products, state);
    setCount(resultsText(list.length));
    results.replaceChildren(list.length
      ? el("div", { className: "products-grid" }, list.map(cardFor))
      : nothingFound());
  }

  function setData(next) {
    if (next !== data) cards.clear();
    data = next;
    states.setBusy(results, false);
    const state = filters.get();
    if (state.type !== "all" && !availableTypes(data.products).includes(state.type)) {
      // Выбранного типа больше нет в каталоге — показываем все товары.
      filters.set({ type: "all" });
    }
    renderChips();
    renderResults();
  }

  function showLoading() {
    states.setBusy(results, true);
    notice.setAttribute("hidden", "");
    setCount("Загружаем каталог…");
    results.replaceChildren(el(
      "div",
      { className: "products-grid" },
      Array.from({ length: skeletonCards }, () => createPlaceholderCard(dom)),
    ));
  }

  function showError(error) {
    states.setBusy(results, false);
    notice.setAttribute("hidden", "");
    setCount("");
    const text = typeof error?.userMessage === "string"
      ? error.userMessage
      : "Не удалось загрузить каталог. Попробуйте ещё раз.";
    // «Повторить» — только повторный GET каталога (force: без кэша). Никаких заказов и оплат.
    const onRetry = () => load({ force: true, fromRetry: true });
    const view = error && NETWORK_CODES.has(error.code)
      ? states.networkError({ title: "Нет связи с магазином", text, onRetry })
      : states.error({ title: "Не удалось загрузить каталог", text, onRetry });
    results.replaceChildren(view);
    return view;
  }

  async function load({ force = false, fromRetry = false } = {}) {
    if (destroyed) return;
    const seq = ++loadSeq;
    const cached = force ? null : source.peek();
    if (cached) {
      setData(cached);
      return;
    }
    if (!fromRetry) showLoading();
    try {
      const result = await source.load({ signal, force });
      if (destroyed || seq !== loadSeq) return;
      setData(result);
      if (fromRetry) heading.focus?.();
    } catch (error) {
      if (destroyed || seq !== loadSeq || isAbortError(error)) return;
      const view = showError(error);
      if (fromRetry) view.querySelector?.(".button-secondary")?.focus?.();
    }
  }

  const unsubscribe = filters.subscribe(() => {
    if (!destroyed) renderResults();
  });

  function destroy() {
    destroyed = true;
    unsubscribe();
  }

  let ready = Promise.resolve();
  if (signal?.aborted) {
    destroy();
  } else {
    signal?.addEventListener("abort", destroy, { once: true });
    renderChips();
    ready = load();
  }

  return Object.freeze({
    node,
    /** Первая загрузка завершена (успешно или с показанной ошибкой). */
    ready,
    destroy,
  });
}
