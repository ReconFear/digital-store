// Тесты js/core/router.js: разбор адреса без исключений и поведение роутера на поддельном window.

import { createRouter, hrefFor, parseRoute, ROUTE_NAMES } from "../../storefront/js/core/router.js";
import { assert, assertEqual, assertThrows } from "./assert.js";

const name = (hash) => parseRoute(hash).name;

function fakeWindow(hash = "") {
  const listeners = new Map();
  return {
    location: { hash },
    addEventListener(type, fn) { listeners.set(type, fn); },
    removeEventListener(type) { listeners.delete(type); },
    navigate(next) {
      this.location.hash = next;
      return listeners.get("hashchange")?.();
    },
    has(type) { return listeners.has(type); },
  };
}

export const tests = [
  ["router: главная", () => {
    for (const hash of ["", "#", "#/", "/"]) assertEqual(name(hash), "home", JSON.stringify(hash));
  }],

  ["router: статические экраны (с завершающим / и без)", () => {
    assertEqual(name("#/catalog"), "catalog");
    assertEqual(name("#/catalog/"), "catalog");
    assertEqual(name("#/cart"), "cart");
    assertEqual(name("#/order"), "order");
    assertEqual(name("#/catalog?type=digital_key"), "catalog", "параметры после ? не мешают");
  }],

  ["router: товар — только положительный числовой id", () => {
    assertEqual(parseRoute("#/product/1"), { name: "product", params: { id: 1 } });
    assertEqual(parseRoute("#/product/2147483647"), { name: "product", params: { id: 2147483647 } });
    for (const hash of ["#/product/0", "#/product/-1", "#/product/01", "#/product/1.5", "#/product/abc",
      "#/product/1e3", "#/product/2147483648", "#/product/99999999999", "#/product/", "#/product",
      "#/product/1/2", "#/product/ 1"]) {
      assertEqual(name(hash), "not_found", hash);
    }
  }],

  ["router: некорректное %-кодирование и мусор — 404 без исключений", () => {
    for (const hash of ["#/product/%E0%A4%A", "#/%", "#/catalog%2F", "#/<script>", "#javascript:alert(1)",
      "#/product/1#x", "#catalog", "#main", "#/unknown", "#//catalog", `#/${"a".repeat(300)}`]) {
      assertEqual(name(hash), "not_found", hash);
    }
    for (const value of [null, undefined, 42, {}]) assertEqual(name(value), "not_found", String(value));
  }],

  ["router: в адресе заказа нет идентификаторов и токенов", () => {
    assertEqual(hrefFor("order"), "#/order");
    for (const hash of ["#/order/42", "#/order?token=abc", "#/order/42/abc"]) {
      const parsed = parseRoute(hash);
      if (hash.includes("?")) {
        assertEqual(parsed, { name: "order", params: {} }, "параметры игнорируются и не попадают в params");
      } else {
        assertEqual(parsed.name, "not_found", hash);
      }
    }
  }],

  ["router: hrefFor совпадает с parseRoute", () => {
    for (const routeName of ["home", "catalog", "cart", "order"]) {
      assertEqual(name(hrefFor(routeName)), routeName);
    }
    assertEqual(parseRoute(hrefFor("product", { id: 7 })), { name: "product", params: { id: 7 } });
    for (const bad of [0, -1, 1.5, "7", null, 2147483648]) {
      assertThrows(() => hrefFor("product", { id: bad }), TypeError, String(bad));
    }
    assertThrows(() => hrefFor("admin"), TypeError);
  }],

  ["router: результат неизменяем и имена из списка", () => {
    const parsed = parseRoute("#/product/3");
    assert(Object.isFrozen(parsed) && Object.isFrozen(parsed.params));
    assert(ROUTE_NAMES.includes(parsed.name));
  }],

  ["router: start рисует текущий экран, hashchange — следующий", async () => {
    const win = fakeWindow("#/cart");
    const seen = [];
    const router = createRouter({ win, render: (route, { initial }) => { seen.push([route.name, initial]); } });
    await router.start();
    await win.navigate("#/product/5");
    await win.navigate("#/%%%");
    assertEqual(seen, [["cart", true], ["product", false], ["not_found", false]]);
    assertEqual(router.current.name, "not_found");
    router.stop();
    assertEqual(win.has("hashchange"), false, "после stop подписки нет");
  }],

  ["router: предыдущий render отменяется через AbortSignal", async () => {
    const win = fakeWindow("#/catalog");
    const signals = [];
    const router = createRouter({
      win,
      render: (route, { signal }) => { signals.push(signal); return new Promise(() => {}); },
    });
    router.start();
    win.navigate("#/cart");
    assertEqual(signals.length, 2);
    assertEqual(signals[0].aborted, true, "первый экран отменён");
    assertEqual(signals[1].aborted, false);
    router.stop();
    assertEqual(signals[1].aborted, true, "stop отменяет текущий");
  }],

  ["router: ошибка render уходит в onError, роутер продолжает работать", async () => {
    const win = fakeWindow("#/");
    const errors = [];
    const router = createRouter({
      win,
      render: (route) => { if (route.name === "cart") throw new Error("boom"); },
      onError: (error, route) => errors.push([error.message, route.name]),
    });
    await router.start();
    await win.navigate("#/cart");
    await win.navigate("#/order");
    assertEqual(errors, [["boom", "cart"]]);
    assertEqual(router.current.name, "order");
    router.stop();
  }],
];
