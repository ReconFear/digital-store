// Тесты js/ui/button.js: варианты, неактивность, загрузка, защита от двойного нажатия.

import { createDom } from "../../storefront/js/core/dom.js";
import { createButton } from "../../storefront/js/ui/button.js";
import { createFakeDocument, byClass } from "./fake-dom.js";
import { assert, assertEqual, assertThrows } from "./assert.js";

const dom = () => createDom(createFakeDocument());

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const isDisabled = (button) => button.node.hasAttribute("disabled");
const spinnerHidden = (button) => byClass(button.node, "button-spinner")[0].hasAttribute("hidden");

export const tests = [
  ["button: основная — класс кнопки «Купить» прототипа, вторичная — кнопки фильтра", () => {
    const primary = createButton(dom(), { label: "Оформить" });
    const secondary = createButton(dom(), { label: "Отмена", variant: "secondary" });
    assertEqual(primary.node.className, "buy-button");
    assertEqual(secondary.node.className, "button-secondary");
    assertEqual(primary.node.tagName, "BUTTON");
    assertEqual(primary.node.getAttribute("type"), "button", "по умолчанию type=button, не submit");
  }],

  ["button: подпись — только текст", () => {
    const button = createButton(dom(), { label: "<img src=x onerror=alert(1)>" });
    const label = byClass(button.node, "button-label")[0];
    assertEqual(label.childNodes.length, 1);
    assertEqual(label.childNodes[0].nodeType, 3);
  }],

  ["button: неактивная кнопка не вызывает обработчик", async () => {
    let calls = 0;
    const button = createButton(dom(), { label: "Купить", disabled: true, onClick: () => { calls += 1; } });
    assert(isDisabled(button));
    await button.press();
    assertEqual(calls, 0);
    button.setDisabled(false);
    assertEqual(isDisabled(button), false);
    await button.press();
    assertEqual(calls, 1);
  }],

  ["button: состояние загрузки — disabled, aria-busy, спиннер и текст для читалок", () => {
    const button = createButton(dom(), { label: "Сохранить", loadingLabel: "Сохраняем…" });
    assertEqual(spinnerHidden(button), true, "спиннер скрыт до загрузки");
    button.setLoading(true);
    assert(isDisabled(button));
    assertEqual(button.node.getAttribute("aria-busy"), "true");
    assertEqual(button.node.className, "buy-button is-loading");
    assertEqual(spinnerHidden(button), false);
    const busyText = byClass(button.node, "visually-hidden")[0];
    assertEqual(busyText.textContent, "Сохраняем…");
    assertEqual(busyText.hasAttribute("hidden"), false);
    assertEqual(byClass(button.node, "button-label")[0].textContent, "Сохранить", "подпись остаётся");
    button.setLoading(false);
    assertEqual(isDisabled(button), false);
    assertEqual(button.node.hasAttribute("aria-busy"), false);
    assertEqual(button.node.className, "buy-button");
  }],

  ["button: после загрузки неактивная кнопка остаётся неактивной", () => {
    const button = createButton(dom(), { label: "Купить", disabled: true });
    button.setLoading(true);
    button.setLoading(false);
    assert(isDisabled(button), "disabled пользователя сохраняется");
  }],

  ["button: асинхронный обработчик — загрузка и защита от двойного нажатия", async () => {
    const task = deferred();
    let calls = 0;
    const button = createButton(dom(), { label: "Повторить", onClick: () => { calls += 1; return task.promise; } });
    const first = button.press();
    assertEqual(button.isLoading(), true);
    button.press();
    button.node.dispatch("click", {});
    assertEqual(calls, 1, "повторные нажатия во время загрузки игнорируются");
    task.resolve();
    await first;
    assertEqual(button.isLoading(), false);
    assertEqual(isDisabled(button), false);
  }],

  ["button: ошибка асинхронного обработчика снимает загрузку", async () => {
    const task = deferred();
    const button = createButton(dom(), { label: "Повторить", onClick: () => task.promise });
    const pressing = button.press();
    task.reject(new Error("network"));
    await pressing;
    assertEqual(button.isLoading(), false);
    assertEqual(isDisabled(button), false);
  }],

  ["button: синхронный обработчик не включает загрузку; исключение не ломает кнопку", async () => {
    let calls = 0;
    const ok = createButton(dom(), { label: "A", onClick: () => { calls += 1; } });
    await ok.press();
    assertEqual(ok.isLoading(), false);
    const failing = createButton(dom(), { label: "B", onClick: () => { throw new Error("x"); } });
    await failing.press();
    assertEqual(failing.isLoading(), false);
    assertEqual(calls, 1);
  }],

  ["button: неверные параметры", () => {
    assertThrows(() => createButton(dom(), { label: "x", variant: "danger" }), TypeError);
    assertThrows(() => createButton(dom(), { label: "" }), TypeError);
    assertThrows(() => createButton(dom(), { label: "x", type: "reset" }), TypeError);
    assertThrows(() => createButton(dom(), { label: "x", onClick: "alert(1)" }), TypeError);
    assertThrows(() => createButton(dom(), { label: "x", attrs: { onclick: "alert(1)" } }), TypeError);
  }],
];
