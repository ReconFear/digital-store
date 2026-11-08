// Бренд магазина: название и тексты. Логика витрины их только показывает.
// Для другого заказчика заменяется эта папка (brand.js + theme.css), а не код.
// Контакты и ссылки не заполнены, пока магазин их не утвердил: ничего не выдумываем.

function deepFreeze(value) {
  for (const item of Object.values(value)) {
    if (item && typeof item === "object") deepFreeze(item);
  }
  return Object.freeze(value);
}

export const BRAND = deepFreeze({
  id: "digital-store",
  name: "Digital Store",
  hero: {
    label: "DIGITAL MARKETPLACE",
    title: "Игры и цифровые товары",
    text: "Выбирай товары, проверяй наличие и оформляй покупку за несколько кликов.",
  },
  // Подписи типов товаров из API (product_type). Категорий в API нет.
  productTypeLabels: {
    digital_key: "Ключи",
    digital_file: "Файлы",
    service: "Услуги",
    physical: "Товары",
    other: "Другое",
  },
  footer: {
    tagline: "Игры и цифровые товары",
  },
  // Примеры формата: { label: "Поддержка", href: "mailto:..." } — добавить после согласования.
  contacts: [],
  links: [],
});
