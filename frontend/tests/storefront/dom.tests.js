// Тесты js/core/dom.js на поддельном DOM. Сеть и браузер не нужны.

import { createDom, clear, safeUrl } from "../../storefront/js/core/dom.js";
import { createFakeDocument, allElements } from "./fake-dom.js";
import { assert, assertEqual, assertThrows } from "./assert.js";

const dom = () => createDom(createFakeDocument());
const MARKUP = '<img src=x onerror="alert(1)"><script>alert(2)</script>';

export const tests = [
  ["dom: текст с разметкой остаётся текстом (через text)", () => {
    const node = dom().el("p", { text: MARKUP });
    assertEqual(node.childNodes.length, 1);
    assertEqual(node.childNodes[0].nodeType, 3, "должен быть текстовый узел");
    assertEqual(node.textContent, MARKUP);
    assertEqual(allElements(node).length, 1, "вложенных элементов быть не должно");
  }],

  ["dom: строковые дети тоже становятся текстовыми узлами", () => {
    const node = dom().el("div", {}, MARKUP, 42, null, false, undefined);
    assertEqual(node.childNodes.length, 2);
    assert(node.childNodes.every((child) => child.nodeType === 3));
    assertEqual(node.textContent, `${MARKUP}42`);
  }],

  ["dom: вложенные массивы детей разворачиваются", () => {
    const { el } = dom();
    const node = el("ul", {}, [el("li", { text: "1" }), [el("li", { text: "2" })]]);
    assertEqual(node.children.map((child) => child.textContent), ["1", "2"]);
  }],

  ["dom: className и обычные атрибуты", () => {
    const node = dom().el("button", { className: "buy-button", attrs: { type: "button", disabled: true, hidden: false } });
    assertEqual(node.className, "buy-button");
    assertEqual(node.getAttribute("type"), "button");
    assertEqual(node.getAttribute("disabled"), "");
    assertEqual(node.hasAttribute("hidden"), false, "false не должен ставить атрибут");
  }],

  ["dom: атрибуты-обработчики on* и style запрещены", () => {
    for (const name of ["onclick", "onerror", "ONLOAD", "style"]) {
      assertThrows(() => dom().el("div", { attrs: { [name]: "alert(1)" } }), TypeError, name);
    }
  }],

  ["dom: обработчики подключаются через addEventListener", () => {
    let clicks = 0;
    const node = dom().el("button", { listeners: { click: () => { clicks += 1; } } });
    node.dispatch("click");
    assertEqual(clicks, 1);
  }],

  ["dom: опасные адреса в href/src отклоняются", () => {
    const bad = [
      "javascript:alert(1)", "JaVaScRiPt:alert(1)", " javascript:alert(1)", "java\tscript:alert(1)",
      "java\nscript:alert(1)", "\u0001javascript:alert(1)", "data:text/html,<b>x</b>", "vbscript:msgbox(1)",
      "", "   ",
    ];
    for (const value of bad) {
      assertEqual(safeUrl(value), null, JSON.stringify(value));
      assertThrows(() => dom().el("a", { attrs: { href: value } }), TypeError, JSON.stringify(value));
    }
    assertThrows(() => dom().svg("image", { "xlink:href": "javascript:alert(1)" }), TypeError);
  }],

  ["dom: безопасные адреса разрешены", () => {
    for (const value of ["#/catalog", "#/product/7", "./index.html", "/storefront/", "https://shop.example/",
      "http://127.0.0.1:5500/", "mailto:help@shop.example", "tel:+70000000000"]) {
      assertEqual(safeUrl(value), value, value);
      assertEqual(dom().el("a", { attrs: { href: value } }).getAttribute("href"), value);
    }
    assertEqual(safeUrl(42), null, "не строка");
  }],

  ["dom: svg создаётся в пространстве имён SVG", () => {
    const icon = dom().svg("svg", { viewBox: "0 0 24 24", "aria-hidden": "true" }, dom().svg("path", { d: "M0 0h24v24H0z" }));
    assertEqual(icon.namespaceURI, "http://www.w3.org/2000/svg");
    assertEqual(icon.children.length, 1);
  }],

  ["dom: clear удаляет содержимое", () => {
    const node = dom().el("div", {}, "a", "b");
    clear(node);
    assertEqual(node.childNodes.length, 0);
  }],

  ["dom: createDom без document — ошибка", () => {
    assertThrows(() => createDom(null), TypeError);
  }],
];
