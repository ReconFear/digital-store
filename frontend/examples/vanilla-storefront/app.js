// Демо-витрина: показывает, как подключить JS-клиент к любой HTML/CSS-разметке.
// Данные API выводятся только через textContent и createElement — HTML-строки не используются.

import { createApiClient, isInStock } from "../../lib/digital-store-api.js";
import { API_BASE_URL } from "./config.js";

const api = createApiClient({ baseUrl: API_BASE_URL });

const statusBox = document.getElementById("status");
const content = document.getElementById("content");
const title = document.getElementById("page-title");
const toolbar = document.getElementById("catalog-toolbar");
const availableOnly = document.getElementById("available-only");

const priceFormat = new Intl.NumberFormat("ru-RU", {
  style: "currency",
  currency: "RUB",
  maximumFractionDigits: 0,
});

let inFlight = null;

// ---------- маленький помощник для безопасного создания элементов ----------

function el(tag, { className, text, attrs } = {}, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  for (const [name, value] of Object.entries(attrs || {})) node.setAttribute(name, value);
  node.append(...children);
  return node;
}

// ---------- состояния: загрузка, пусто, ошибка ----------

function showLoading(message) {
  content.setAttribute("aria-busy", "true");
  statusBox.replaceChildren(el("p", { className: "loading", text: message }));
}

function clearStatus() {
  content.removeAttribute("aria-busy");
  statusBox.replaceChildren();
}

function showMessage(message, { kind = "info", action } = {}) {
  content.removeAttribute("aria-busy");
  const box = el("div", { className: `message message--${kind}` }, el("p", { text: message }));
  if (action) box.append(action);
  statusBox.replaceChildren(box);
}

function showError(error, retry) {
  const userMessage = error.userMessage || "Произошла ошибка.";
  const box = el("div", { className: "message message--error" }, el("p", { text: userMessage }));

  if (retry) {
    const button = el("button", { className: "button", text: "Повторить", attrs: { type: "button" } });
    button.addEventListener("click", retry);
    box.append(button);
  }

  // Подсказка для разработчика (адрес backend, HTTP-статус). Для покупателей в своей теме её можно убрать.
  const tech = el("details", { className: "tech" },
    el("summary", { text: "Подробности для разработчика" }),
    el("p", { text: error.message }),
  );
  if (error.code === "network") {
    tech.append(el("p", {
      text: `Проверьте: запущен ли backend на ${api.baseUrl}; открыта ли страница через HTTP-сервер ` +
        `(а не как файл); разрешён ли origin ${location.origin} в CORS_ALLOWED_ORIGINS.`,
    }));
  }
  box.append(tech);

  content.removeAttribute("aria-busy");
  statusBox.replaceChildren(box);
}

function startRequest() {
  inFlight?.abort();
  inFlight = new AbortController();
  return inFlight.signal;
}

// ---------- каталог ----------

function productCard(product) {
  const inStock = isInStock(product);
  return el("article", { className: "card" },
    el("h2", { className: "card__title", text: product.name }),
    el("p", { className: "card__price", text: priceFormat.format(product.price) }),
    el("p", {
      className: inStock ? "card__stock" : "card__stock card__stock--out",
      text: inStock ? "В наличии" : "Нет в наличии",
    }),
    el("a", { className: "card__link", text: "Подробнее", attrs: { href: `#/product/${product.id}` } }),
  );
}

async function showCatalog() {
  toolbar.hidden = false;
  title.textContent = "Каталог";
  document.title = "Digital Store — демо-витрина";
  content.replaceChildren();
  showLoading("Загружаем каталог…");

  const signal = startRequest();
  try {
    const { products } = await api.getProducts({ availableOnly: availableOnly.checked, signal });
    if (products.length === 0) {
      showMessage(
        availableOnly.checked ? "Сейчас нет товаров в наличии." : "В каталоге пока нет товаров.",
        { kind: "empty" },
      );
      return;
    }
    clearStatus();
    content.replaceChildren(el("div", { className: "grid" }, ...products.map(productCard)));
  } catch (error) {
    if (error.code === "aborted") return;
    showError(error, showCatalog);
  }
}

// ---------- один товар ----------

function backLink() {
  return el("a", { className: "button button--ghost", text: "← К каталогу", attrs: { href: "#/" } });
}

async function showProduct(rawId) {
  toolbar.hidden = true;
  title.textContent = "Товар";
  content.replaceChildren();
  showLoading("Загружаем товар…");

  const signal = startRequest();
  try {
    const product = await api.getProduct(rawId, { signal });
    const inStock = isInStock(product);
    clearStatus();
    title.textContent = product.name;
    document.title = `${product.name} — Digital Store`;
    content.replaceChildren(
      el("div", { className: "product" },
        el("p", { className: "product__price", text: priceFormat.format(product.price) }),
        el("p", {
          className: inStock ? "card__stock" : "card__stock card__stock--out",
          text: inStock ? "В наличии" : "Нет в наличии",
        }),
        el("p", {
          className: "note",
          text: "Покупка в этой версии недоступна: оплата и выдача товара ещё не реализованы.",
        }),
        backLink(),
      ),
    );
  } catch (error) {
    if (error.code === "aborted") return;
    if (error.code === "not_found" || error.code === "invalid_argument") {
      showMessage("Товар не найден или больше не продаётся.", { kind: "empty", action: backLink() });
      return;
    }
    showError(error, () => showProduct(rawId));
  }
}

// ---------- маршрутизация по #хешу (работает на любом статическом сервере) ----------

function route() {
  const match = location.hash.match(/^#\/product\/([^/]+)$/);
  if (match) showProduct(decodeURIComponent(match[1]));
  else showCatalog();
}

availableOnly.addEventListener("change", showCatalog);
window.addEventListener("hashchange", route);
route();
