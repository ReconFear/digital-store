// Маршрутизация по #хешу: работает на любом статическом хостинге.
// В адресе не бывает секретов: номер и токен заказа в URL не передаются.

export const ROUTE_NAMES = Object.freeze(["home", "catalog", "product", "cart", "order", "not_found"]);

const MAX_HASH_LENGTH = 200;
const MAX_PRODUCT_ID = 2_147_483_647;
const PRODUCT_ID_RE = /^[1-9][0-9]{0,9}$/;

const STATIC_ROUTES = new Map([
  ["", "home"],
  ["catalog", "catalog"],
  ["cart", "cart"],
  ["order", "order"],
]);

function notFound() {
  return Object.freeze({ name: "not_found", params: Object.freeze({}) });
}

function route(name, params = {}) {
  return Object.freeze({ name, params: Object.freeze(params) });
}

/**
 * Разбирает location.hash. Никогда не бросает исключений:
 * всё неизвестное, слишком длинное или с %-кодированием — "not_found".
 * Параметры после "?" в T1 игнорируются (появятся для фильтров каталога).
 */
export function parseRoute(hash) {
  if (typeof hash !== "string" || hash.length > MAX_HASH_LENGTH) return notFound();

  let path = hash.startsWith("#") ? hash.slice(1) : hash;
  const query = path.indexOf("?");
  if (query !== -1) path = path.slice(0, query);

  if (path === "" || path === "/") return route("home");
  if (!path.startsWith("/")) return notFound();
  if (/[^A-Za-z0-9/_-]/.test(path)) return notFound();

  const parts = path.slice(1).replace(/\/$/, "").split("/");

  if (parts.length === 1 && STATIC_ROUTES.has(parts[0])) {
    return route(STATIC_ROUTES.get(parts[0]));
  }
  if (parts.length === 2 && parts[0] === "product" && PRODUCT_ID_RE.test(parts[1])) {
    const id = Number(parts[1]);
    if (Number.isSafeInteger(id) && id <= MAX_PRODUCT_ID) return route("product", { id });
  }
  return notFound();
}

/** Адрес для ссылки на экран. */
export function hrefFor(name, params = {}) {
  switch (name) {
    case "home": return "#/";
    case "catalog": return "#/catalog";
    case "cart": return "#/cart";
    case "order": return "#/order";
    case "product": {
      const id = params.id;
      if (!Number.isSafeInteger(id) || id < 1 || id > MAX_PRODUCT_ID) {
        throw new TypeError(`hrefFor: неверный id товара ${String(id)}`);
      }
      return `#/product/${id}`;
    }
    default: throw new TypeError(`hrefFor: неизвестный экран ${String(name)}`);
  }
}

/**
 * Подписывается на hashchange и вызывает render(route, { signal, initial }).
 * Предыдущий незавершённый render отменяется через AbortSignal.
 * Ошибки render передаются в onError и не ломают навигацию.
 */
export function createRouter({ win, render, onError = () => {} }) {
  if (!win || typeof render !== "function") throw new TypeError("createRouter: нужны win и render");
  let controller = null;
  let current = null;
  let started = false;

  async function handle(initial) {
    controller?.abort();
    controller = new AbortController();
    const { signal } = controller;
    current = parseRoute(win.location.hash);
    try {
      await render(current, { signal, initial });
    } catch (error) {
      if (!signal.aborted) onError(error, current);
    }
  }

  const listener = () => { handle(false); };

  return Object.freeze({
    start() {
      if (started) return Promise.resolve();
      started = true;
      win.addEventListener("hashchange", listener);
      return handle(true);
    },
    stop() {
      started = false;
      win.removeEventListener("hashchange", listener);
      controller?.abort();
    },
    get current() {
      return current;
    },
  });
}
