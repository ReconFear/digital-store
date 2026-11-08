// Источник каталога: один GET /api/v2/products на всех потребителей.
//  - кэш в памяти страницы на ttlMs (поиск и фильтры работают по нему, без новых запросов);
//  - одновременные загрузки (главная, каталог, «Повторить») ждут один и тот же запрос;
//  - потребитель отменяет ожидание своим AbortSignal (уход со страницы); сам запрос
//    отменяется, только когда его больше никто не ждёт;
//  - в кэш попадает ответ только последнего начатого запроса — устаревший ответ не перезапишет новый;
//  - ошибки не кэшируются: «Повторить» снова выполняет только GET каталога.

const PRODUCT_FIELDS = ["id", "name", "product_type", "inventory_mode", "stock_quantity", "available"];

export function abortError() {
  const error = new Error("Загрузка каталога отменена");
  error.name = "AbortError";
  return error;
}

export function isAbortError(error) {
  return Boolean(error) && error.name === "AbortError";
}

/** Копия только подтверждённых полей API v2, неизменяемая. Лишние поля ответа не используются. */
function freezeProduct(product) {
  const copy = {};
  for (const field of PRODUCT_FIELDS) copy[field] = product[field];
  const { amount_minor, currency, exponent } = product.price;
  copy.price = Object.freeze({ amount_minor, currency, exponent });
  return Object.freeze(copy);
}

/**
 * @param {object} options
 * @param {{ getProductsV2: Function }} options.api
 * @param {number} [options.ttlMs] сколько миллисекунд ответ считается свежим
 * @param {() => number} [options.now] часы (в тестах — поддельные)
 * @param {(fn: Function) => void} [options.defer] отложить проверку «запрос больше никому не нужен»
 */
export function createCatalogSource({ api, ttlMs = 60000, now = () => Date.now(), defer = queueMicrotask } = {}) {
  if (!api || typeof api.getProductsV2 !== "function") throw new TypeError("createCatalogSource: нужен api.getProductsV2");

  let cache = null;
  let inflight = null;
  let generation = 0;

  function fresh() {
    return cache && now() - cache.at < ttlMs ? cache.data : null;
  }

  function start() {
    const id = ++generation;
    const entry = { controller: new AbortController(), consumers: 0, promise: null };
    entry.promise = Promise.resolve()
      .then(() => api.getProductsV2({ signal: entry.controller.signal }))
      .then((result) => {
        const data = Object.freeze({
          products: Object.freeze(result.products.map(freezeProduct)),
          total: result.total,
        });
        if (id === generation) cache = { data, at: now() };
        return data;
      })
      .finally(() => {
        if (inflight === entry) inflight = null;
      });
    inflight = entry;
    return entry;
  }

  function release(entry) {
    entry.consumers -= 1;
    if (entry.consumers > 0) return;
    // Проверка откладывается: при переходе «главная -> каталог» новый экран успевает
    // подключиться к тому же запросу, и второй запрос не нужен.
    defer(() => {
      if (entry.consumers === 0 && inflight === entry) {
        inflight = null;
        entry.controller.abort();
      }
    });
  }

  /**
   * Каталог { products, total }. force: true — не брать кэш (кнопка «Повторить»).
   * Отмена signal отклоняет обещание с AbortError и ничего не показывает.
   */
  function load({ signal, force = false } = {}) {
    if (signal?.aborted) return Promise.reject(abortError());
    if (!force) {
      const data = fresh();
      if (data) return Promise.resolve(data);
    }
    const entry = inflight ?? start();
    entry.consumers += 1;

    return new Promise((resolve, reject) => {
      let settled = false;
      const onAbort = () => {
        if (settled) return;
        settled = true;
        release(entry);
        reject(abortError());
      };
      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        entry.consumers -= 1;
        signal?.removeEventListener("abort", onAbort);
        callback(value);
      };
      signal?.addEventListener("abort", onAbort, { once: true });
      entry.promise.then((data) => finish(resolve, data), (error) => finish(reject, error));
    });
  }

  return Object.freeze({
    load,
    /** Свежие данные из кэша или null — чтобы показать каталог сразу, без мигания загрузки. */
    peek: fresh,
    get pending() {
      return inflight !== null;
    },
  });
}
