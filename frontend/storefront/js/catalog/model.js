// Каталог: чистые функции поиска, фильтров и сортировки. Без DOM и без сети:
// всё считается на клиенте по уже загруженному списку GET /api/v2/products.
// Используются только поля API v2: id, name, product_type, inventory_mode, price, stock_quantity, available.

/** Типы товаров по контракту API v2 — в порядке показа чипов. */
export const PRODUCT_TYPES = Object.freeze(["digital_key", "digital_file", "service", "physical", "other"]);

export const MAX_QUERY_LENGTH = 100;

export const SORTS = Object.freeze([
  Object.freeze({ id: "default", label: "По умолчанию" }),
  Object.freeze({ id: "price_asc", label: "Сначала дешевле" }),
  Object.freeze({ id: "price_desc", label: "Сначала дороже" }),
  Object.freeze({ id: "name_asc", label: "По названию (А–Я)" }),
]);

const SORT_IDS = new Set(SORTS.map((sort) => sort.id));

export const DEFAULT_FILTERS = Object.freeze({
  query: "",
  type: "all",
  inStockOnly: false,
  sort: "default",
});

const nameCollator = new Intl.Collator("ru", { numeric: true, sensitivity: "base" });
const countFormat = new Intl.NumberFormat("ru-RU");

/** Текст для сравнения: без учёта регистра, «ё» = «е», лишние пробелы схлопнуты. */
export function normalizeText(value) {
  return String(value ?? "")
    .toLocaleLowerCase("ru-RU")
    .replace(/ё/g, "е")
    .replace(/\s+/g, " ")
    .trim();
}

/** Поисковый запрос: нормализован и ограничен по длине. Регулярные выражения не используются. */
export function normalizeQuery(value) {
  return normalizeText(value).slice(0, MAX_QUERY_LENGTH).trim();
}

/** Неизвестный (будущий) тип относится к группе «Другое». */
export function typeGroup(productType) {
  return PRODUCT_TYPES.includes(productType) ? productType : "other";
}

export function typeLabel(productType, labels = {}) {
  return labels[typeGroup(productType)] ?? labels.other ?? "Другое";
}

/** Группы типов, которые реально есть в списке, — в порядке PRODUCT_TYPES. */
export function availableTypes(products) {
  const present = new Set(products.map((product) => typeGroup(product.product_type)));
  return PRODUCT_TYPES.filter((type) => present.has(type));
}

export function isSortId(value) {
  return SORT_IDS.has(value);
}

/**
 * Сравнение цен Money без дробных чисел: amount_minor приводится к общему exponent
 * в BigInt. Разные валюты (в контракте сейчас только RUB) упорядочиваются по коду валюты.
 */
export function compareMoney(a, b) {
  if (a.currency !== b.currency) return a.currency < b.currency ? -1 : 1;
  if (a.exponent === b.exponent) return Math.sign(a.amount_minor - b.amount_minor);
  const exponent = Math.max(a.exponent, b.exponent);
  if (exponent - Math.min(a.exponent, b.exponent) > 30) {
    // Нереалистичная разница точности: сравниваем приближённо, без огромных степеней.
    return Math.sign(a.amount_minor / 10 ** a.exponent - b.amount_minor / 10 ** b.exponent);
  }
  const scale = (money) => BigInt(money.amount_minor) * 10n ** BigInt(exponent - money.exponent);
  const left = scale(a);
  const right = scale(b);
  return left === right ? 0 : left < right ? -1 : 1;
}

export function compareNames(a, b) {
  return nameCollator.compare(a, b);
}

const COMPARATORS = {
  price_asc: (a, b) => compareMoney(a.price, b.price),
  price_desc: (a, b) => compareMoney(b.price, a.price),
  name_asc: (a, b) => compareNames(a.name, b.name),
};

/**
 * Новый отсортированный массив; исходный не меняется. «По умолчанию» — порядок ответа API.
 * При равенстве — по id, чтобы порядок был стабильным. Наличие на порядок не влияет.
 */
export function sortProducts(products, sort = "default") {
  const list = [...products];
  const compare = COMPARATORS[sort];
  if (!compare) return list;
  return list.sort((a, b) => compare(a, b) || a.id - b.id);
}

export function matchesQuery(product, normalizedQuery) {
  return !normalizedQuery || normalizeText(product.name).includes(normalizedQuery);
}

/** Поиск + тип + «Только в наличии» (по полю available) + сортировка. */
export function selectProducts(products, filters = DEFAULT_FILTERS) {
  const query = normalizeQuery(filters.query);
  const type = filters.type ?? "all";
  const result = products.filter((product) =>
    (type === "all" || typeGroup(product.product_type) === type)
    && (!filters.inStockOnly || product.available === true)
    && matchesQuery(product, query));
  return sortProducts(result, isSortId(filters.sort) ? filters.sort : "default");
}

export function isDefaultFilters(filters) {
  return normalizeQuery(filters.query) === ""
    && (filters.type ?? "all") === "all"
    && !filters.inStockOnly
    && (filters.sort ?? "default") === "default";
}

export function formatCount(value) {
  return countFormat.format(value);
}

/**
 * Подпись наличия. Решение «в наличии» — только по available.
 * Остаток показывается для учитываемых товаров (tracked); у unlimited stock_quantity = null.
 */
export function stockInfo(product) {
  if (product.available !== true) return { available: false, text: "Нет в наличии" };
  if (product.inventory_mode === "tracked" && Number.isSafeInteger(product.stock_quantity) && product.stock_quantity > 0) {
    return { available: true, text: `В наличии · ${formatCount(product.stock_quantity)} шт.` };
  }
  return { available: true, text: "В наличии" };
}

/** Русское склонение: plural(5, ["товар", "товара", "товаров"]) -> "товаров". */
export function plural(count, [one, few, many]) {
  const mod10 = count % 10;
  const mod100 = count % 100;
  if (mod10 === 1 && mod100 !== 11) return one;
  if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return few;
  return many;
}

export function resultsText(count) {
  return `Найдено: ${formatCount(count)} ${plural(count, ["товар", "товара", "товаров"])}`;
}
