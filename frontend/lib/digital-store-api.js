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
  orders_disabled: "Оформление заказов отключено.",
  aborted: "Запрос отменён.",
});

/**
 * Единый тип ошибки клиента.
 *  - code: "network" | "timeout" | "aborted" | "not_found" | "unavailable" | "validation"
 *          | "forbidden" | "server" | "http" | "invalid_response" | "invalid_argument" | "orders_disabled"
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
      const code = STATUS_CODES[response.status] || (response.status >= 500 ? "server" : "http");
      const detail = data && typeof data === "object" ? data.detail ?? null : null;
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
  });
}

/** Удобная проверка наличия: товар в продаже и остаток больше нуля. */
export function isInStock(product) {
  return Boolean(product && product.is_for_sale && product.quantity > 0);
}
