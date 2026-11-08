// Состояния интерфейса: загрузка, пусто, ошибка, сетевая ошибка, «скоро» и информационные сообщения.
// Только текст, без HTML-строк. Функции T1 (loading, notice, empty, error, actionLink) сохранили
// прежние параметры и классы; в T2 добавлены message, networkError и setBusy.

import { createButton } from "./button.js";

export const MESSAGE_TONES = Object.freeze(["info", "success", "warning", "error"]);

export function createStates(dom) {
  const { el } = dom;

  function loading(text = "Загрузка…") {
    return el("p", { className: "state state--loading", text, attrs: { role: "status" } });
  }

  /** Помечает область как обновляющуюся (aria-busy) на время загрузки. */
  function setBusy(container, busy) {
    if (busy) container.setAttribute("aria-busy", "true");
    else container.removeAttribute("aria-busy");
  }

  /** Сообщение о том, что раздел ещё не подключён. */
  function notice({ badge = "Скоро", text }) {
    return el(
      "div",
      { className: "notice", attrs: { role: "note" } },
      el("span", { className: "notice-badge", text: badge }),
      el("p", { text }),
    );
  }

  /** actions: [{ label, href }] — ссылки-действия под сообщением. */
  function empty({ title, text, actions = [] }) {
    return el(
      "div",
      { className: "state" },
      title ? el("p", { className: "state-title", text: title }) : null,
      text ? el("p", { text }) : null,
      actions.length ? el("div", { className: "state-actions" }, actions.map(actionLink)) : null,
    );
  }

  /** Кнопка «Повторить»: пока onRetry выполняется (Promise), кнопка в загрузке и не нажимается. */
  function retryButton(onRetry, label = "Повторить") {
    return createButton(dom, { label, variant: "secondary", onClick: onRetry, loadingLabel: "Повторяем…" });
  }

  /** onRetry — необязательная функция для кнопки «Повторить». */
  function error({ title = "Что-то пошло не так", text, onRetry }) {
    return el(
      "div",
      { className: "state state--error", attrs: { role: "alert" } },
      el("p", { className: "state-title", text: title }),
      text ? el("p", { text }) : null,
      onRetry ? el("div", { className: "state-actions" }, retryButton(onRetry).node) : null,
    );
  }

  /**
   * Информационное сообщение: tone — info | success | warning | error.
   * live: true — сообщение появилось в ответ на действие и должно быть объявлено
   * (error -> role="alert", остальные -> role="status"); false — часть страницы.
   * actions: [{ label, href }] — ссылки под сообщением; buttons — готовые узлы кнопок.
   */
  function message({ tone = "info", title, text, actions = [], buttons = [], live = false }) {
    if (!MESSAGE_TONES.includes(tone)) throw new TypeError(`message: неизвестный тип ${String(tone)}`);
    if (!title && !text) throw new TypeError("message: нужен title или text");
    const role = live ? (tone === "error" ? "alert" : "status") : null;
    const controls = [...actions.map(actionLink), ...buttons];
    return el(
      "div",
      { className: `message message--${tone}`, attrs: { role } },
      title ? el("p", { className: "message-title", text: title }) : null,
      text ? el("p", { className: "message-text", text }) : null,
      controls.length ? el("div", { className: "state-actions" }, controls) : null,
    );
  }

  /**
   * Сетевая ошибка с кнопкой «Повторить». onRetry должен только повторить ЧТЕНИЕ данных
   * (загрузку экрана) — не создавать заказы и не запускать оплату.
   */
  function networkError({
    title = "Нет связи с магазином",
    text = "Проверьте подключение к интернету и попробуйте ещё раз.",
    onRetry,
  } = {}) {
    if (typeof onRetry !== "function") throw new TypeError("networkError: нужен onRetry");
    return message({ tone: "error", title, text, buttons: [retryButton(onRetry).node], live: true });
  }

  function actionLink({ label, href }) {
    return el("a", { className: "button-secondary", text: label, attrs: { href } });
  }

  return Object.freeze({ loading, setBusy, notice, empty, error, message, networkError, actionLink });
}
