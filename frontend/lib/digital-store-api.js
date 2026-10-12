/**
 * Digital Store — JavaScript-клиент публичного API.
 *
 * Независимый ES-модуль: без зависимостей, без DOM, без CSS. Подходит для любой
 * HTML/CSS-темы. Отражает ТОЛЬКО реально существующие эндпоинты backend:
 *
 *   GET  /products[?available_only=true]  -> { products: Product[], total: number }
 *   GET  /products/{id}                   -> Product            (404, если нет или снят с продажи)
 *   POST /orders  { product_id }          -> { message, order }  (создаёт заказ "pending", БЕЗ оплаты)
 *
 * Product: { id: number, name: string, price: number, quantity: number, is_for_sale: boolean }
 * price — целое число в рублях (см. FRONTEND_API.md).
 *
 * API v2 (универсальное ядро, PR 2) — отдельные методы с суффиксом V2:
 *   GET  /api/v2/products[?available_only=true] -> { products: ProductV2[], total }
 *   GET  /api/v2/products/{id}                  -> ProductV2
 *   POST /api/v2/orders { items: [{ product_id, quantity }] } -> 201 { order } (pending, БЕЗ оплаты)
 * ProductV2: { id, name, product_type, inventory_mode, price: Money, stock_quantity: number|null, available }
 * Money: { amount_minor: целое (копейки), currency: "RUB", exponent: 2 }
 *
 * Пример:
 *   import { createApiClient } from "./digital-store-api.js";
 *   const api = createApiClient({ baseUrl: "http://127.0.0.1:8000" });
 *   const { products } = await api.getProducts();
 */

export const DEFAULT_BASE_URL = "http://127.0.0.1:8000";
export const DEFAULT_TIMEOUT_MS = 10000;

/** Пути API. Тест tests/test_frontend_contract.py сверяет их с api.py. */
export const ENDPOINTS = Object.freeze({
  products: "/products",
  product: "/products/{id}",
  orders: "/orders",
  productsV2: "/api/v2/products",
  productV2: "/api/v2/products/{id}",
  ordersV2: "/api/v2/orders",
});

/** Тексты для покупателя. Тема оформления может заменить их своими по полю `code`. */
export const USER_MESSAGES = Object.freeze({
  network: "Не удалось связаться с сервером магазина. Проверьте подключение и попробуйте ещё раз.",
  timeout: "Сервер магазина не ответил вовремя. Попробуйте ещё раз.",
  not_found: "Товар не найден или больше не продаётся.",
  unavailable: "Этот товар сейчас недоступен для заказа.",
  validation: "Запрос заполнен неверно.",
  forbidden: "Доступ запрещён.",
  server: "На сервере магазина произошла ошибка. Попробуйте позже.",
  http: "Сервер магазина вернул ошибку.",
  invalid_response: "Сервер магазина вернул неожиданный ответ.",
  invalid_argument: "Неверные данные запроса.",
  insufficient_stock: "Недостаточно товара в наличии. Уменьшите количество.",
  mixed_currency: "Эти товары нельзя оформить одним заказом.",
  unsupported_currency: "Этот товар сейчас нельзя заказать.",
  orders_disabled: "Оформление заказов отключено.",
  aborted: "Запрос отменён.",
});

/**
 * Единый тип ошибки клиента.
 *  - code: "network" | "timeout" | "aborted" | "not_found" | "unavailable" | "validation"
 *          | "forbidden" | "server" | "http" | "invalid_response" | "invalid_argument" | "orders_disabled"
 *          | "insufficient_stock" | "mixed_currency" | "unsupported_currency" (только API v2)
 *  - status: HTTP-статус или null, если ответа не было
 *  - detail: поле `detail` из ответа backend (строка или массив ошибок валидации) или null
 *  - userMessage: готовый текст для интерфейса
 */
export class DigitalStoreApiError extends Error {
  constructor(code, { status = null, detail = null, message, cause } = {}) {
    const userMessage = USER_MESSAGES[code] || USER_MESSAGES.http;
    super(message || userMessage, cause ? { cause } : undefined);
    this.name = "DigitalStoreApiError";
    this.code = code;
    this.status = status;
    this.detail = detail;
    this.userMessage = userMessage;
  }

  /** true, если имеет смысл предложить кнопку «Повторить». */
  get retryable() {
    return ["network", "timeout", "server"].includes(this.code);
  }
}

const STATUS_CODES = { 403: "forbidden", 404: "not_found", 409: "unavailable", 422: "validation" };

// API v2 сообщает точную причину в detail.code; эти коды клиент передаёт как есть.
const V2_DETAIL_CODES = ["insufficient_stock", "mixed_currency", "unsupported_currency"];

function normalizeBaseUrl(baseUrl) {
  if (typeof baseUrl !== "string" || !baseUrl.trim()) {
    throw new TypeError("createApiClient: укажите baseUrl, например http://127.0.0.1:8000");
  }
  let url;
  try {
    url = new URL(baseUrl.trim());
  } catch {
    throw new TypeError(`createApiClient: baseUrl "${baseUrl}" не является адресом`);
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new TypeError("createApiClient: baseUrl должен начинаться с http:// или https://");
  }
  if (url.search || url.hash || url.username || url.password) {
    throw new TypeError("createApiClient: baseUrl не должен содержать параметры, # или логин");
  }
  return url.href.replace(/\/+$/, "");
}

function isPositiveId(value) {
  return Number.isSafeInteger(value) && value > 0;
}

function isMoney(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    Number.isSafeInteger(value.amount_minor) &&
    value.amount_minor >= 0 &&
    typeof value.currency === "string" &&
    Number.isSafeInteger(value.exponent) &&
    value.exponent >= 0
  );
}

function isProductV2(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    isPositiveId(value.id) &&
    typeof value.name === "string" &&
    typeof value.product_type === "string" &&
    (value.inventory_mode === "tracked" || value.inventory_mode === "unlimited") &&
    isMoney(value.price) &&
    (value.stock_quantity === null || Number.isSafeInteger(value.stock_quantity)) &&
    typeof value.available === "boolean"
  );
}

function isOrderV2(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    isPositiveId(value.id) &&
    typeof value.status === "string" &&
    isMoney(value.total) &&
    Array.isArray(value.items) &&
    value.items.length > 0 &&
    value.items.every(
      (item) =>
        item &&
        isPositiveId(item.product_id) &&
        typeof item.name === "string" &&
        Number.isSafeInteger(item.quantity) &&
        isMoney(item.unit_price) &&
        isMoney(item.line_total),
    )
  );
}

function toProductId(id) {
  return typeof id === "string" && /^\d+$/.test(id) ? Number(id) : id;
}

function isProduct(value) {
  return (
    value !== null &&
    typeof value === "object" &&
    isPositiveId(value.id) &&
    typeof value.name === "string" &&
    typeof value.price === "number" &&
    typeof value.quantity === "number" &&
    typeof value.is_for_sale === "boolean"
  );
}

/**
 * Создаёт клиент API.
 *
 * @param {object} [options]
 * @param {string} [options.baseUrl] адрес backend, по умолчанию DEFAULT_BASE_URL
 * @param {number} [options.timeoutMs] таймаут одного запроса
 * @param {boolean} [options.allowOrderCreation] разрешить createOrder(); по умолчанию false
 * @param {Function} [options.fetch] своя реализация fetch (для тестов)
 */
export function createApiClient(options = {}) {
  const baseUrl = normalizeBaseUrl(options.baseUrl ?? DEFAULT_BASE_URL);
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const allowOrderCreation = options.allowOrderCreation === true;
  const fetchImpl = options.fetch ?? globalThis.fetch?.bind(globalThis);

  if (typeof fetchImpl !== "function") {
    throw new TypeError("createApiClient: в этой среде нет fetch");
  }

  async function request(path, { method = "GET", body, signal } = {}) {
    const controller = new AbortController();
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);
    const onExternalAbort = () => controller.abort();
    if (signal) {
      if (signal.aborted) controller.abort();
      else signal.addEventListener("abort", onExternalAbort, { once: true });
    }

    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";

    let response;
    try {
      response = await fetchImpl(baseUrl + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: "omit",
        signal: controller.signal,
      });
    } catch (cause) {
      if (timedOut) throw new DigitalStoreApiError("timeout", { cause });
      if (signal?.aborted) throw new DigitalStoreApiError("aborted", { cause });
      // Браузер не различает «сервер выключен», «неверный адрес» и «CORS запретил» —
      // во всех случаях fetch бросает TypeError. Подробности — в консоли браузера.
      throw new DigitalStoreApiError("network", {
        cause,
        message:
          `Нет ответа от ${baseUrl}. Проверьте, что backend запущен, адрес верный ` +
          "и origin этой страницы разрешён в CORS_ALLOWED_ORIGINS.",
      });
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", onExternalAbort);
    }

    let data = null;
    const text = await response.text().catch(() => "");
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = undefined;
      }
    }

    if (!response.ok) {
      const detail = data && typeof data === "object" ? data.detail ?? null : null;
      const v2Code = detail && typeof detail === "object" && !Array.isArray(detail) ? detail.code : null;
      const code = V2_DETAIL_CODES.includes(v2Code)
        ? v2Code
        : STATUS_CODES[response.status] || (response.status >= 500 ? "server" : "http");
      throw new DigitalStoreApiError(code, {
        status: response.status,
        detail,
        message: `${method} ${path}: HTTP ${response.status}` + (typeof detail === "string" ? ` — ${detail}` : ""),
      });
    }
    if (data === undefined || data === null) {
      throw new DigitalStoreApiError("invalid_response", {
        status: response.status,
        message: `${method} ${path}: ответ не является JSON`,
      });
    }
    return data;
  }

  function invalidResponse(path, what) {
    return new DigitalStoreApiError("invalid_response", { message: `${path}: ${what}` });
  }

  return Object.freeze({
    baseUrl,

    /**
     * Каталог: товары в продаже (включая закончившиеся).
     * @param {{ availableOnly?: boolean, signal?: AbortSignal }} [params]
     * @returns {Promise<{ products: object[], total: number }>}
     */
    async getProducts({ availableOnly = false, signal } = {}) {
      const path = ENDPOINTS.products + (availableOnly ? "?available_only=true" : "");
      const data = await request(path, { signal });
      if (!Array.isArray(data.products) || typeof data.total !== "number" || !data.products.every(isProduct)) {
        throw invalidResponse(path, "неожиданный формат каталога");
      }
      return { products: data.products, total: data.total };
    },

    /**
     * Один товар. Если товара нет или он снят с продажи — DigitalStoreApiError с code "not_found".
     * @param {number|string} id положительное целое
     */
    async getProduct(id, { signal } = {}) {
      const numericId = typeof id === "string" && /^\d+$/.test(id) ? Number(id) : id;
      if (!isPositiveId(numericId)) {
        throw new DigitalStoreApiError("invalid_argument", { message: `getProduct: неверный id ${String(id)}` });
      }
      const path = ENDPOINTS.product.replace("{id}", String(numericId));
      const data = await request(path, { signal });
      if (!isProduct(data)) throw invalidResponse(path, "неожиданный формат товара");
      return data;
    },

    /**
     * Создаёт НАСТОЯЩИЙ заказ в базе в статусе "pending" (ожидает оплаты).
     * Оплаты, резервирования и выдачи товара пока нет — это не покупка.
     * Отключено, пока клиент не создан с allowOrderCreation: true.
     * @param {number} productId
     * @returns {Promise<{ message: string, order: { id: number, product_id: number, price: number, status: string } }>}
     */
    async createOrder(productId, { signal } = {}) {
      if (!allowOrderCreation) {
        throw new DigitalStoreApiError("orders_disabled", {
          message: "createOrder отключён: создайте клиент с allowOrderCreation: true (только для тестов)",
        });
      }
      if (!isPositiveId(productId)) {
        throw new DigitalStoreApiError("invalid_argument", {
          message: `createOrder: неверный productId ${String(productId)}`,
        });
      }
      const data = await request(ENDPOINTS.orders, { method: "POST", body: { product_id: productId }, signal });
      const order = data.order;
      if (
        typeof data.message !== "string" ||
        !order ||
        !isPositiveId(order.id) ||
        order.product_id !== productId ||
        typeof order.price !== "number" ||
        typeof order.status !== "string"
      ) {
        throw invalidResponse(ENDPOINTS.orders, "неожиданный формат заказа");
      }
      return { message: data.message, order };
    },

    // ---------------- API v2 ----------------

    /**
     * Каталог v2: товары в продаже, включая товары без учёта остатка (услуги, файлы).
     * @returns {Promise<{ products: object[], total: number }>}
     */
    async getProductsV2({ availableOnly = false, signal } = {}) {
      const path = ENDPOINTS.productsV2 + (availableOnly ? "?available_only=true" : "");
      const data = await request(path, { signal });
      if (!Array.isArray(data.products) || typeof data.total !== "number" || !data.products.every(isProductV2)) {
        throw invalidResponse(path, "неожиданный формат каталога v2");
      }
      return { products: data.products, total: data.total };
    },

    /** Один товар v2. Нет или снят с продажи — ошибка с code "not_found". */
    async getProductV2(id, { signal } = {}) {
      const numericId = toProductId(id);
      if (!isPositiveId(numericId)) {
        throw new DigitalStoreApiError("invalid_argument", { message: `getProductV2: неверный id ${String(id)}` });
      }
      const path = ENDPOINTS.productV2.replace("{id}", String(numericId));
      const data = await request(path, { signal });
      if (!isProductV2(data)) throw invalidResponse(path, "неожиданный формат товара v2");
      return data;
    },

    /**
     * Создаёт НАСТОЯЩИЙ заказ из нескольких позиций в статусе "pending" (ожидает оплаты).
     * Передаются только product_id и quantity; цены и итог считает сервер.
     * Оплаты, резерва и выдачи пока нет. Отключено без allowOrderCreation: true.
     * @param {Array<{ product_id: number, quantity: number }>} items
     * @returns {Promise<{ order: object }>}
     */
    async createOrderV2(items, { signal } = {}) {
      if (!allowOrderCreation) {
        throw new DigitalStoreApiError("orders_disabled", {
          message: "createOrderV2 отключён: создайте клиент с allowOrderCreation: true",
        });
      }
      const valid =
        Array.isArray(items) &&
        items.length > 0 &&
        items.every(
          (item) =>
            item && isPositiveId(item.product_id) && Number.isSafeInteger(item.quantity) && item.quantity > 0,
        );
      if (!valid) {
        throw new DigitalStoreApiError("invalid_argument", {
          message: "createOrderV2: нужен непустой массив { product_id, quantity } с положительными целыми",
        });
      }
      const body = { items: items.map(({ product_id, quantity }) => ({ product_id, quantity })) };
      const data = await request(ENDPOINTS.ordersV2, { method: "POST", body, signal });
      if (!isOrderV2(data.order)) throw invalidResponse(ENDPOINTS.ordersV2, "неожиданный формат заказа v2");
      return { order: data.order };
    },
  });
}

/**
 * Форматирует сумму API v2 для показа: formatMoney({ amount_minor: 99950, currency: "RUB", exponent: 2 })
 * -> "999,50 ₽". Только для отображения: итог заказа всегда считает сервер.
 * Целые копейки переводятся в десятичную строку без деления float.
 */
export function formatMoney(money, locale = "ru-RU") {
  if (!isMoney(money)) throw new TypeError("formatMoney: ожидается { amount_minor, currency, exponent }");
  const { amount_minor: minor, currency, exponent } = money;
  const digits = String(minor).padStart(exponent + 1, "0");
  const decimal = exponent ? `${digits.slice(0, -exponent)}.${digits.slice(-exponent)}` : digits;
  const fraction = minor % 10 ** exponent === 0 ? 0 : exponent;
  return new Intl.NumberFormat(locale, {
    style: "currency",
    currency,
    minimumFractionDigits: fraction,
    maximumFractionDigits: exponent,
  }).format(decimal);
}

/** Удобная проверка наличия: товар в продаже и остаток больше нуля. */
export function isInStock(product) {
  return Boolean(product && product.is_for_sale && product.quantity > 0);
}
