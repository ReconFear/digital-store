// Кнопки Digital Store: основная (градиентная «Купить» прототипа) и вторичная (кнопка фильтра).
// Состояния: обычное, неактивное, загрузка. Подпись — только текст.
//
// Если onClick возвращает Promise, кнопка сама переходит в загрузку до его завершения
// и не принимает повторные нажатия — защита от двойного действия.

const VARIANT_CLASSES = Object.freeze({
  primary: "buy-button",
  secondary: "button-secondary",
});

/**
 * @param {object} dom результат createDom(document)
 * @param {object} options
 * @param {string} options.label подпись (текст)
 * @param {"primary"|"secondary"} [options.variant]
 * @param {"button"|"submit"} [options.type]
 * @param {boolean} [options.disabled]
 * @param {(event) => (void|Promise)} [options.onClick]
 * @param {string} [options.loadingLabel] текст для экранных читалок во время загрузки
 * @param {object} [options.attrs] дополнительные атрибуты (проверяются dom.el)
 */
export function createButton(dom, {
  label,
  variant = "primary",
  type = "button",
  disabled = false,
  onClick,
  loadingLabel = "Загрузка…",
  attrs = {},
} = {}) {
  const className = VARIANT_CLASSES[variant];
  if (!className) throw new TypeError(`createButton: неизвестный вариант ${String(variant)}`);
  if (typeof label !== "string" || !label.trim()) throw new TypeError("createButton: нужна подпись");
  if (type !== "button" && type !== "submit") throw new TypeError("createButton: type — button или submit");
  if (onClick !== undefined && typeof onClick !== "function") throw new TypeError("createButton: onClick — функция");

  const { el } = dom;
  const spinner = el("span", { className: "button-spinner", attrs: { "aria-hidden": "true", hidden: true } });
  const busyText = el("span", { className: "visually-hidden", text: loadingLabel, attrs: { hidden: true } });
  const node = el(
    "button",
    { className, attrs: { ...attrs, type } },
    spinner,
    el("span", { className: "button-label", text: label }),
    busyText,
  );

  let userDisabled = Boolean(disabled);
  let loading = false;

  function applyState() {
    if (userDisabled || loading) node.setAttribute("disabled", "");
    else node.removeAttribute("disabled");

    if (loading) {
      node.setAttribute("aria-busy", "true");
      node.className = `${className} is-loading`;
      spinner.removeAttribute("hidden");
      busyText.removeAttribute("hidden");
    } else {
      node.removeAttribute("aria-busy");
      node.className = className;
      spinner.setAttribute("hidden", "");
      busyText.setAttribute("hidden", "");
    }
  }

  function setLoading(value) {
    loading = Boolean(value);
    applyState();
  }

  function setDisabled(value) {
    userDisabled = Boolean(value);
    applyState();
  }

  /** Нажатие: вызывает onClick, для Promise держит загрузку. Возвращает Promise для тестов. */
  async function press(event) {
    if (loading || userDisabled || !onClick) return;
    let result;
    try {
      result = onClick(event);
    } catch {
      return;
    }
    if (!result || typeof result.then !== "function") return;
    setLoading(true);
    try {
      await result;
    } catch {
      // Ошибку показывает вызывающий код; кнопка просто снова становится доступной.
    } finally {
      setLoading(false);
    }
  }

  node.addEventListener("click", (event) => { press(event); });
  applyState();

  return Object.freeze({
    node,
    setLoading,
    setDisabled,
    press,
    isLoading: () => loading,
  });
}
