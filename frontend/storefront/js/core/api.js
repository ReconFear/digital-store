// Доступ витрины к API. Общий клиент frontend/lib/digital-store-api.js не меняется:
// витрина получает от него только чтение каталога. Клиент создаётся без разрешения на заказы,
// поэтому заказы и оплата технически заблокированы.

import { createApiClient, formatMoney, DigitalStoreApiError } from "../../../lib/digital-store-api.js";

export { formatMoney, DigitalStoreApiError };

/**
 * @param {object} config CONFIG витрины (apiBaseUrl, requestTimeoutMs)
 * @param {object} [options]
 * @param {Function} [options.fetch] поддельный fetch для тестов
 */
export function createStoreApi(config, { fetch } = {}) {
  const client = createApiClient({
    baseUrl: config.apiBaseUrl,
    timeoutMs: config.requestTimeoutMs,
    ...(fetch ? { fetch } : {}),
  });

  return Object.freeze({
    baseUrl: client.baseUrl,
    /** GET /api/v2/products — весь каталог. Поиск и фильтры считаются на клиенте. */
    getProductsV2: ({ signal } = {}) => client.getProductsV2({ signal }),
    getProductV2: (id, { signal } = {}) => client.getProductV2(id, { signal }),
  });
}
