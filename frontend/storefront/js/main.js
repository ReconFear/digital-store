// Запуск витрины Digital Store: бренд -> шапка и подвал -> область уведомлений -> маршрутизатор.
// T3: главная и каталог загружают товары через GET /api/v2/products (только чтение).
// Поиск и фильтры общие для главной и каталога, хранятся в памяти страницы и не делают запросов.
// Корзина, заказы и оплата не подключены: клиент API создан без разрешения на заказы.
import { createCart } from "./core/cart.js";
import { CONFIG } from "../config.js";
import { BRAND } from "../brand/brand.js";
import { createDom } from "./core/dom.js";
import { createRouter, hrefFor } from "./core/router.js";
import { createStore } from "./core/store.js";
import { createStoreApi } from "./core/api.js";
import { createCatalogSource } from "./catalog/source.js";
import { DEFAULT_FILTERS } from "./catalog/model.js";
import { createHeader } from "./ui/header.js";
import { createFooter } from "./ui/footer.js";
import { createStates } from "./ui/states.js";
import { createToaster } from "./ui/toast.js";
import { createScreens } from "./pages/stubs.js";

const dom = createDom(document);
const main = document.getElementById("main");
const headerRoot = document.getElementById("site-header");
const footerRoot = document.getElementById("site-footer");
const skipLink = document.getElementById("skip-link");

const appState = createStore({ route: null });
const cartStore = createCart();
const catalogFilters = createStore({ ...DEFAULT_FILTERS });
const storeApi = createStoreApi(CONFIG);

const catalog = CONFIG.features.catalog
  ? { source: createCatalogSource({ api: storeApi, ttlMs: CONFIG.catalogCacheMs }), filters: catalogFilters }
  : null;

const header = createHeader({
  dom,
  brand: BRAND,
  config: CONFIG,
  search: {
    onInput: (query) => catalogFilters.set({ query }),
    // Enter на экране без каталога (корзина, заказ, товар) открывает каталог с этим запросом.
    onSubmit: () => {
      const name = appState.get().route?.name;
      if (name !== "home" && name !== "catalog") window.location.hash = hrefFor("catalog");
    },
  },
});
const footer = createFooter({ dom, brand: BRAND });
const screens = createScreens({
  dom,
  brand: BRAND,
  catalog,
  api: storeApi,
  cartStore,
  onAdd: CONFIG.features.cart
    ? (productId) => cartStore.add(productId)
    : null,
});
const states = createStates(dom);

headerRoot.replaceChildren(header.node);
footerRoot.replaceChildren(footer.node);
appState.subscribe((state) => header.setActive(state.route?.name));
catalogFilters.subscribe((state) => header.setSearchValue(state.query));

// Область toast-уведомлений создаётся сразу при запуске: так экранные читалки
// надёжно объявляют уведомления, добавленные позже. В T2 магазин уведомлений не показывает.
createToaster({ dom, root: document.body, keyTarget: document });

// Ссылка «Перейти к содержимому» не должна менять #хеш (иначе сработает маршрутизатор).
skipLink.addEventListener("click", (event) => {
  event.preventDefault();
  main.focus();
});

function focusPageHeading() {
  const heading = main.querySelector("h1");
  window.scrollTo(0, 0);
  if (heading) {
    heading.setAttribute("tabindex", "-1");
    heading.focus({ preventScroll: true });
  } else {
    main.focus({ preventScroll: true });
  }
}

// Сигнал текущего экрана: маршрутизатор отменяет его при переходе (нужен и для «Повторить»).
let currentSignal;

function render(route, { initial, signal }) {
  currentSignal = signal;
  const screen = screens[route.name] || screens.not_found;
  const view = screen(route.params, { signal });
  main.replaceChildren(view.node);
  document.title = view.title ? `${view.title} — ${BRAND.name}` : BRAND.name;
  appState.set({ route });
  // При первой загрузке фокус не переносим: читалка начинает со страницы целиком.
  if (!initial) focusPageHeading();
}

// «Повторить» только заново рисует текущий экран: никаких заказов и оплат
// (экран каталога при этом может повторить GET каталога).
function showRenderError(route, signal) {
  main.replaceChildren(states.error({
    title: "Не удалось показать страницу",
    text: "Попробуйте ещё раз или вернитесь на главную.",
    onRetry: () => {
      try {
        render(route, { initial: false, signal });
      } catch {
        showRenderError(route, signal);
        main.querySelector(".button-secondary")?.focus();
      }
    },
  }));
}

const router = createRouter({
  win: window,
  render,
  onError: (error, route) => showRenderError(route, currentSignal),
});

router.start();
