// Тесты js/core/storage.js (браузерное хранилище) и js/core/store.js (состояние в памяти).

import { createStorage } from "../../storefront/js/core/storage.js";
import { createStore } from "../../storefront/js/core/store.js";
import { assert, assertEqual, assertThrows } from "./assert.js";

function memoryStorage() {
  const data = new Map();
  return {
    data,
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => { data.set(key, String(value)); },
    removeItem: (key) => { data.delete(key); },
  };
}

function throwingStorage() {
  const fail = () => { throw new Error("SecurityError"); };
  return { getItem: fail, setItem: fail, removeItem: fail };
}

function make(backendValue, extra = {}) {
  return createStorage({ backend: () => backendValue, prefix: "ds:test:", ...extra });
}

export const tests = [
  ["storage: запись и чтение с префиксом и версией", () => {
    const mem = memoryStorage();
    const storage = make(mem);
    assertEqual(storage.writeJson("cart", [{ product_id: 1, quantity: 2 }]), true);
    assertEqual(storage.readJson("cart"), [{ product_id: 1, quantity: 2 }]);
    assertEqual([...mem.data.keys()], ["ds:test:cart"]);
    assertEqual(JSON.parse(mem.data.get("ds:test:cart")).v, 1);
  }],

  ["storage: нет записи — fallback", () => {
    assertEqual(make(memoryStorage()).readJson("cart", { fallback: [] }), []);
  }],

  ["storage: хранилище недоступно (бросает при обращении) — не падает", () => {
    const storage = createStorage({ backend: () => { throw new Error("denied"); }, prefix: "ds:test:" });
    assertEqual(storage.readJson("cart", { fallback: "fb" }), "fb");
    assertEqual(storage.writeJson("cart", [1]), false);
    storage.remove("cart");
    assertEqual(storage.isAvailable(), false);
  }],

  ["storage: методы хранилища бросают (приватный режим, переполнение) — не падает", () => {
    const storage = make(throwingStorage());
    assertEqual(storage.readJson("cart", { fallback: null }), null);
    assertEqual(storage.writeJson("cart", [1]), false);
    storage.remove("cart");
    assertEqual(storage.isAvailable(), false);
    assertEqual(make(null).readJson("cart", { fallback: 0 }), 0, "хранилища нет совсем");
  }],

  ["storage: повреждённые и чужие данные игнорируются и удаляются", () => {
    for (const raw of ["{not json", "null", "42", '"text"', '{"v":2,"exp":null,"data":1}',
      '{"v":1,"exp":"soon","data":1}', '{"v":1,"exp":null}']) {
      const mem = memoryStorage();
      mem.data.set("ds:test:cart", raw);
      assertEqual(make(mem).readJson("cart", { fallback: "fb" }), "fb", raw);
      assertEqual(mem.data.has("ds:test:cart"), false, `запись должна быть удалена: ${raw}`);
    }
  }],

  ["storage: validate отклоняет неподходящие данные", () => {
    const mem = memoryStorage();
    const storage = make(mem);
    storage.writeJson("cart", { evil: true });
    const isArray = (value) => Array.isArray(value);
    assertEqual(storage.readJson("cart", { validate: isArray, fallback: [] }), []);
    assertEqual(mem.data.size, 0);
    storage.writeJson("cart", [1]);
    assertEqual(storage.readJson("cart", { validate: () => { throw new Error("x"); }, fallback: [] }), []);
  }],

  ["storage: срок хранения (ttl)", () => {
    let now = 1_000;
    const mem = memoryStorage();
    const storage = make(mem, { now: () => now });
    storage.writeJson("recent", "value", { ttlMs: 500 });
    assertEqual(storage.readJson("recent"), "value");
    now = 1_499;
    assertEqual(storage.readJson("recent"), "value");
    now = 1_500;
    assertEqual(storage.readJson("recent", { fallback: "expired" }), "expired");
    assertEqual(mem.data.size, 0, "истёкшая запись удалена");
    for (const bad of [0, -1, Infinity, NaN, "10"]) {
      assertThrows(() => storage.writeJson("recent", 1, { ttlMs: bad }), TypeError, String(bad));
    }
  }],

  ["storage: недопустимые ключи и значения", () => {
    const storage = make(memoryStorage());
    for (const bad of ["", "a b", "../x", "ключ", null, "x".repeat(65)]) {
      assertThrows(() => storage.readJson(bad), TypeError, String(bad));
    }
    const cyclic = {};
    cyclic.self = cyclic;
    assertEqual(storage.writeJson("cycle", cyclic), false, "несериализуемое значение");
    assertEqual(storage.writeJson("undef", undefined), false, "undefined не записывается");
    assertThrows(() => createStorage({ backend: null, prefix: "x" }), TypeError);
    assertThrows(() => createStorage({ backend: () => null, prefix: "" }), TypeError);
  }],

  ["storage: isAvailable на рабочем хранилище не оставляет следов", () => {
    const mem = memoryStorage();
    assertEqual(make(mem).isAvailable(), true);
    assertEqual(mem.data.size, 0);
  }],

  ["store: get/set/subscribe и неизменяемость", () => {
    const store = createStore({ route: null, count: 0 });
    const seen = [];
    const unsubscribe = store.subscribe((state, previous) => seen.push([previous.count, state.count]));
    store.set({ count: 1 });
    store.set((state) => ({ count: state.count + 1 }));
    unsubscribe();
    store.set({ count: 10 });
    assertEqual(seen, [[0, 1], [1, 2]]);
    assertEqual(store.get(), { route: null, count: 10 });
    assert(Object.isFrozen(store.get()));
  }],

  ["store: ошибка подписчика не мешает остальным; неверные аргументы", () => {
    const store = createStore();
    let called = false;
    store.subscribe(() => { throw new Error("bad listener"); });
    store.subscribe(() => { called = true; });
    store.set({ a: 1 });
    assert(called);
    assertThrows(() => store.set(null), TypeError);
    assertThrows(() => store.set([1]), TypeError);
    assertThrows(() => store.subscribe("x"), TypeError);
    assertThrows(() => createStore([]), TypeError);
  }],
];
