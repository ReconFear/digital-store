// Минимальный поддельный DOM для unit-тестов в Node.js (без зависимостей).
// Поддерживает только то, что используют модули витрины: элементы, текстовые узлы,
// атрибуты, append/replaceChildren/remove, textContent, addEventListener и
// события документа (T2: клавиша Esc для toast).
// Текст хранится как есть и никогда не разбирается как HTML — так тесты видят,
// что строка с тегами осталась текстом.

class FakeText {
  constructor(data) {
    this.nodeType = 3;
    this.data = String(data);
    this.parentNode = null;
  }

  get textContent() {
    return this.data;
  }
}

function attach(parent, node) {
  const child = typeof node === "string" ? new FakeText(node) : node;
  if (child.parentNode && child.parentNode !== parent) child.parentNode.removeChildNode(child);
  child.parentNode = parent;
  return child;
}

class FakeEventTarget {
  constructor() {
    this.listeners = new Map();
  }

  addEventListener(type, handler) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(handler);
  }

  dispatch(type, event = {}) {
    for (const handler of this.listeners.get(type) || []) handler(event);
    return event;
  }
}

class FakeElement extends FakeEventTarget {
  constructor(tagName, namespaceURI = null) {
    super();
    this.nodeType = 1;
    this.tagName = String(tagName).toUpperCase();
    this.localName = String(tagName);
    this.namespaceURI = namespaceURI;
    this.attributes = new Map();
    this.childNodes = [];
    this.parentNode = null;
  }

  get className() {
    return this.attributes.get("class") ?? "";
  }

  set className(value) {
    this.attributes.set("class", String(value));
  }

  get children() {
    return this.childNodes.filter((node) => node.nodeType === 1);
  }

  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  hasAttribute(name) {
    return this.attributes.has(name);
  }

  removeAttribute(name) {
    this.attributes.delete(name);
  }

  append(...nodes) {
    for (const node of nodes) this.childNodes.push(attach(this, node));
  }

  replaceChildren(...nodes) {
    for (const child of this.childNodes) child.parentNode = null;
    this.childNodes = [];
    this.append(...nodes);
  }

  removeChildNode(child) {
    this.childNodes = this.childNodes.filter((node) => node !== child);
    child.parentNode = null;
  }

  remove() {
    this.parentNode?.removeChildNode(this);
  }

  get textContent() {
    return this.childNodes.map((node) => node.textContent).join("");
  }

  set textContent(value) {
    this.replaceChildren(new FakeText(value));
  }
}

export function createFakeDocument() {
  const doc = new FakeEventTarget();
  doc.createElement = (tag) => new FakeElement(tag);
  doc.createElementNS = (ns, tag) => new FakeElement(tag, ns);
  doc.createTextNode = (text) => new FakeText(text);
  doc.body = new FakeElement("body");
  return doc;
}

/** Все элементы поддерева (включая корень) — для проверок в тестах. */
export function allElements(root) {
  const result = [];
  const walk = (node) => {
    if (node.nodeType !== 1) return;
    result.push(node);
    node.childNodes.forEach(walk);
  };
  walk(root);
  return result;
}

/** Элементы поддерева, у которых есть CSS-класс name. */
export function byClass(root, name) {
  return allElements(root).filter((node) => node.className.split(/\s+/).includes(name));
}

/** Поддельные таймеры: schedule/cancel для компонентов и run() для «прокрутки» времени. */
export function fakeTimers() {
  let now = 0;
  let nextId = 1;
  const timers = new Map();
  return {
    schedule(fn, ms) {
      const id = nextId++;
      timers.set(id, { fn, at: now + ms });
      return id;
    },
    cancel(id) {
      timers.delete(id);
    },
    advance(ms) {
      now += ms;
      for (const [id, timer] of [...timers]) {
        if (timer.at <= now) {
          timers.delete(id);
          timer.fn();
        }
      }
    },
    get pending() {
      return timers.size;
    },
  };
}
