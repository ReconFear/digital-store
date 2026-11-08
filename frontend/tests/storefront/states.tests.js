// Тесты js/ui/states.js: прежние состояния T1, сообщения T2 и сетевая ошибка с «Повторить».

import { createDom } from "../../storefront/js/core/dom.js";
import { createStates } from "../../storefront/js/ui/states.js";
import { createFakeDocument, byClass, allElements } from "./fake-dom.js";
import { assert, assertEqual, assertThrows } from "./assert.js";

const states = () => createStates(createDom(createFakeDocument()));

export const tests = [
  ["states: функции T1 сохранили классы и роли", () => {
    const s = states();
    const loading = s.loading();
    assertEqual([loading.tagName, loading.className, loading.getAttribute("role"), loading.textContent],
      ["P", "state state--loading", "status", "Загрузка…"]);
    const notice = s.notice({ text: "Скоро будет" });
    assertEqual([notice.className, notice.getAttribute("role")], ["notice", "note"]);
    assertEqual(byClass(notice, "notice-badge")[0].textContent, "Скоро");
    const empty = s.empty({ title: "Пусто", text: "Нет", actions: [{ label: "К каталогу", href: "#/catalog" }] });
    assertEqual(empty.className, "state");
    assertEqual(byClass(empty, "button-secondary")[0].getAttribute("href"), "#/catalog");
    const error = s.error({ text: "Сбой" });
    assertEqual([error.className, error.getAttribute("role")], ["state state--error", "alert"]);
    assertEqual(byClass(error, "state-title")[0].textContent, "Что-то пошло не так");
  }],

  ["states: error с onRetry — кнопка «Повторить» на компоненте кнопки", async () => {
    let calls = 0;
    const error = states().error({ text: "Сбой", onRetry: () => { calls += 1; } });
    const [button] = byClass(error, "button-secondary");
    assertEqual(button.tagName, "BUTTON");
    assertEqual(button.getAttribute("type"), "button");
    assertEqual(byClass(button, "button-label")[0].textContent, "Повторить");
    button.dispatch("click", {});
    await Promise.resolve();
    assertEqual(calls, 1);
  }],

  ["states: message — четыре типа, классы и текст", () => {
    const s = states();
    for (const tone of ["info", "success", "warning", "error"]) {
      const node = s.message({ tone, title: `T ${tone}`, text: "<i>текст</i>" });
      assertEqual(node.className, `message message--${tone}`);
      assertEqual(byClass(node, "message-title")[0].textContent, `T ${tone}`);
      const text = byClass(node, "message-text")[0];
      assertEqual(text.childNodes[0].nodeType, 3, "текст, а не разметка");
      assertEqual(node.hasAttribute("role"), false, "статичное сообщение не объявляется");
    }
  }],

  ["states: live-сообщения — alert для ошибки, status для остальных", () => {
    const s = states();
    assertEqual(s.message({ tone: "error", text: "x", live: true }).getAttribute("role"), "alert");
    assertEqual(s.message({ tone: "success", text: "x", live: true }).getAttribute("role"), "status");
  }],

  ["states: message — ссылки и кнопки, неверные параметры", () => {
    const s = states();
    const node = s.message({ text: "x", actions: [{ label: "На главную", href: "#/" }] });
    assertEqual(byClass(node, "button-secondary")[0].getAttribute("href"), "#/");
    assertThrows(() => s.message({ tone: "danger", text: "x" }), TypeError);
    assertThrows(() => s.message({ tone: "info" }), TypeError);
    assertThrows(() => s.message({ text: "x", actions: [{ label: "x", href: "javascript:alert(1)" }] }), TypeError);
  }],

  ["states: networkError — ошибка с «Повторить», повтор не запускается дважды", async () => {
    let calls = 0;
    let finish;
    const node = states().networkError({
      onRetry: () => { calls += 1; return new Promise((resolve) => { finish = resolve; }); },
    });
    assertEqual(node.className, "message message--error");
    assertEqual(node.getAttribute("role"), "alert");
    assertEqual(byClass(node, "message-title")[0].textContent, "Нет связи с магазином");
    const [button] = allElements(node).filter((el) => el.tagName === "BUTTON");
    button.dispatch("click", {});
    button.dispatch("click", {});
    assertEqual(calls, 1, "второе нажатие во время повтора игнорируется");
    assertEqual(button.getAttribute("aria-busy"), "true");
    finish();
    await new Promise((resolve) => setTimeout(resolve, 0));
    assertEqual(button.hasAttribute("aria-busy"), false);
    assertThrows(() => states().networkError({}), TypeError, "без onRetry нельзя");
  }],

  ["states: setBusy ставит и снимает aria-busy", () => {
    const s = states();
    const area = createDom(createFakeDocument()).el("section");
    s.setBusy(area, true);
    assertEqual(area.getAttribute("aria-busy"), "true");
    s.setBusy(area, false);
    assert(!area.hasAttribute("aria-busy"));
  }],
];
