// Toast-уведомления Digital Store.
// * Содержимое — только текст (textContent), HTML не разбирается.
// * Область объявлений aria-live="polite"; ошибка — role="alert", остальные — role="status".
// * Фокус не перехватывается. Закрытие — кнопкой «×» или клавишей Esc.
// * info / success / warning закрываются сами (по умолчанию через 5 с);
//   таймер стоит на паузе, пока указатель или фокус внутри уведомления.
// * Ошибки сами НЕ закрываются (решение по T2): покупатель должен успеть прочитать.
// * Одновременно — не больше max уведомлений; лишнее убирается начиная со старых не-ошибок.

export const TOAST_TONES = Object.freeze(["info", "success", "warning", "error"]);

/**
 * @param {object} options
 * @param {object} options.dom результат createDom(document)
 * @param {object} options.root куда добавить область уведомлений (document.body)
 * @param {object} [options.keyTarget] где слушать Esc (document); не задан — Esc работает внутри области
 * @param {Function} [options.schedule] (fn, ms) => id — для тестов вместо setTimeout
 * @param {Function} [options.cancel] (id) => void — для тестов вместо clearTimeout
 * @param {number} [options.timeoutMs] время показа info/success/warning
 * @param {number} [options.max] сколько уведомлений видно одновременно
 */
export function createToaster({
  dom,
  root,
  keyTarget = null,
  schedule = (fn, ms) => setTimeout(fn, ms),
  cancel = (id) => clearTimeout(id),
  timeoutMs = 5000,
  max = 3,
} = {}) {
  if (!dom || !root) throw new TypeError("createToaster: нужны dom и root");
  if (!(Number.isFinite(timeoutMs) && timeoutMs > 0)) throw new TypeError("createToaster: timeoutMs > 0");
  if (!(Number.isInteger(max) && max > 0)) throw new TypeError("createToaster: max — целое > 0");

  const { el } = dom;
  const region = el("div", {
    className: "toast-region",
    attrs: { "aria-live": "polite", "aria-relevant": "additions", "aria-label": "Уведомления" },
  });
  root.append(region);

  const toasts = new Map(); // id -> { node, tone, timer, autoClose }
  let nextId = 1;

  function startTimer(entry) {
    if (!entry.autoClose || entry.timer !== null) return;
    entry.timer = schedule(() => dismiss(entry.id), entry.timeoutMs);
  }

  function stopTimer(entry) {
    if (entry.timer !== null) {
      cancel(entry.timer);
      entry.timer = null;
    }
  }

  function dismiss(id) {
    const entry = toasts.get(id);
    if (!entry) return false;
    stopTimer(entry);
    toasts.delete(id);
    entry.node.remove();
    return true;
  }

  function enforceLimit() {
    while (toasts.size > max) {
      const ids = [...toasts.keys()];
      const oldestRegular = ids.find((id) => toasts.get(id).tone !== "error");
      dismiss(oldestRegular ?? ids[0]);
    }
  }

  /**
   * Показывает уведомление. Возвращает id для dismiss(id).
   * @param {{ tone?: string, title?: string, text: string, timeoutMs?: number }} options
   */
  function show({ tone = "info", title, text, timeoutMs: ownTimeout } = {}) {
    if (!TOAST_TONES.includes(tone)) throw new TypeError(`toast: неизвестный тип ${String(tone)}`);
    if (typeof text !== "string" || !text.trim()) throw new TypeError("toast: нужен текст");
    if (title !== undefined && typeof title !== "string") throw new TypeError("toast: title — строка");
    const duration = ownTimeout ?? timeoutMs;
    if (!(Number.isFinite(duration) && duration > 0)) throw new TypeError("toast: timeoutMs > 0");

    const id = nextId++;
    const closeButton = el("button", {
      className: "toast-close",
      text: "×",
      attrs: { type: "button", "aria-label": "Закрыть уведомление" },
      listeners: { click: () => dismiss(id) },
    });
    const node = el(
      "div",
      { className: `toast toast--${tone}`, attrs: { role: tone === "error" ? "alert" : "status", "data-toast-id": String(id) } },
      el(
        "div",
        { className: "toast-body" },
        title ? el("p", { className: "toast-title", text: title }) : null,
        el("p", { className: "toast-text", text }),
      ),
      closeButton,
    );

    const entry = { id, node, tone, timer: null, autoClose: tone !== "error", timeoutMs: duration };
    node.addEventListener("mouseenter", () => stopTimer(entry));
    node.addEventListener("mouseleave", () => startTimer(entry));
    node.addEventListener("focusin", () => stopTimer(entry));
    node.addEventListener("focusout", () => startTimer(entry));

    toasts.set(id, entry);
    region.append(node);
    startTimer(entry);
    enforceLimit();
    return id;
  }

  function dismissNewest() {
    const ids = [...toasts.keys()];
    return ids.length ? dismiss(ids[ids.length - 1]) : false;
  }

  function onKeydown(event) {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    if (dismissNewest() && typeof event.preventDefault === "function") event.preventDefault();
  }

  (keyTarget || region).addEventListener("keydown", onKeydown);

  return Object.freeze({
    region,
    show,
    dismiss,
    dismissAll() {
      for (const id of [...toasts.keys()]) dismiss(id);
    },
    count: () => toasts.size,
  });
}
