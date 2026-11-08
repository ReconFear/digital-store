// Безопасное создание DOM. Данные попадают на страницу только как текст
// (createTextNode / textContent) и через проверенные атрибуты. HTML-строки не разбираются.

const URL_ATTRIBUTES = new Set(["href", "src", "action", "formaction", "xlink:href", "poster", "srcset"]);
const ALLOWED_SCHEMES = new Set(["http:", "https:", "mailto:", "tel:"]);
const SVG_NS = "http://www.w3.org/2000/svg";

/**
 * Ссылка, которую можно поставить в href/src, или null.
 * Разрешены относительные адреса (#/…, ./…, /…) и схемы http, https, mailto, tel.
 * Пробелы и управляющие символы удаляются перед проверкой — браузер тоже их игнорирует,
 * поэтому "java\tscript:" и " javascript:" считаются javascript:.
 */
export function safeUrl(value) {
  if (typeof value !== "string") return null;
  const compact = value.replace(/[\u0000-\u0020\u007f-\u009f]/g, "");
  if (!compact) return null;
  const scheme = /^([a-z][a-z0-9+.-]*):/i.exec(compact);
  if (!scheme) return value.trim();
  return ALLOWED_SCHEMES.has(scheme[1].toLowerCase() + ":") ? value.trim() : null;
}

function isEventAttribute(name) {
  return /^on/i.test(name);
}

function setAttributes(node, attrs) {
  for (const [name, raw] of Object.entries(attrs || {})) {
    if (raw === null || raw === undefined || raw === false) continue;
    if (isEventAttribute(name) || name.toLowerCase() === "style") {
      throw new TypeError(`dom: атрибут ${name} запрещён — используйте addEventListener и CSS-классы`);
    }
    let value = raw === true ? "" : String(raw);
    if (URL_ATTRIBUTES.has(name.toLowerCase())) {
      const safe = safeUrl(value);
      if (safe === null) throw new TypeError(`dom: небезопасный адрес в атрибуте ${name}`);
      value = safe;
    }
    node.setAttribute(name, value);
  }
}

function appendChildren(doc, node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false || child === true) continue;
    if (typeof child === "string" || typeof child === "number") {
      node.append(doc.createTextNode(String(child)));
    } else {
      node.append(child);
    }
  }
}

/** Помощники, привязанные к конкретному document (в тестах — к поддельному). */
export function createDom(doc) {
  if (!doc || typeof doc.createElement !== "function") {
    throw new TypeError("createDom: нужен document");
  }

  /**
   * el("a", { className, text, attrs, listeners }, ...children)
   * text — только текст; attrs — обычные атрибуты (без on* и style);
   * listeners — { click: fn } через addEventListener.
   */
  function el(tag, props = {}, ...children) {
    const node = doc.createElement(tag);
    if (props.className) node.className = props.className;
    setAttributes(node, props.attrs);
    for (const [type, handler] of Object.entries(props.listeners || {})) {
      node.addEventListener(type, handler);
    }
    if (props.text !== undefined && props.text !== null) {
      node.textContent = String(props.text);
    }
    appendChildren(doc, node, children);
    return node;
  }

  /** SVG-элемент для иконок: только атрибуты фигур, без скриптов и ссылок. */
  function svg(tag, attrs = {}, ...children) {
    const node = doc.createElementNS(SVG_NS, tag);
    setAttributes(node, attrs);
    appendChildren(doc, node, children);
    return node;
  }

  return { el, svg, clear, text: (value) => doc.createTextNode(String(value)) };
}

export function clear(node) {
  node.replaceChildren();
  return node;
}
