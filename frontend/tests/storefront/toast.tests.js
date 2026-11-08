// Тесты js/ui/toast.js на поддельном DOM и поддельных таймерах.

import { createDom } from "../../storefront/js/core/dom.js";
import { createToaster } from "../../storefront/js/ui/toast.js";
import { createFakeDocument, byClass, fakeTimers } from "./fake-dom.js";
import { assert, assertEqual, assertThrows } from "./assert.js";

function setup(options = {}) {
  const doc = createFakeDocument();
  const timers = fakeTimers();
  const toaster = createToaster({
    dom: createDom(doc),
    root: doc.body,
    keyTarget: doc,
    schedule: timers.schedule,
    cancel: timers.cancel,
    ...options,
  });
  return { doc, timers, toaster };
}

const toastsIn = (toaster) => byClass(toaster.region, "toast");

export const tests = [
  ["toast: область уведомлений добавлена в root и объявляется вежливо", () => {
    const { doc, toaster } = setup();
    assert(doc.body.children[0] === toaster.region, "область — первый элемент root");
    assertEqual(toaster.region.getAttribute("aria-live"), "polite");
    assertEqual(toaster.region.className, "toast-region");
  }],

  ["toast: текст и заголовок — только текст, не разметка", () => {
    const { toaster } = setup();
    toaster.show({ title: "<b>Заголовок</b>", text: "<script>alert(1)</script>" });
    const [toast] = toastsIn(toaster);
    const text = byClass(toast, "toast-text")[0];
    assertEqual(text.childNodes.length, 1);
    assertEqual(text.childNodes[0].nodeType, 3);
    assertEqual(text.textContent, "<script>alert(1)</script>");
    assertEqual(byClass(toast, "toast-title")[0].textContent, "<b>Заголовок</b>");
  }],

  ["toast: роли — error как alert, остальные как status; класс по типу", () => {
    const { toaster } = setup({ max: 4 });
    for (const tone of ["info", "success", "warning", "error"]) toaster.show({ tone, text: tone });
    assertEqual(toastsIn(toaster).map((t) => [t.className, t.getAttribute("role")]), [
      ["toast toast--info", "status"], ["toast toast--success", "status"],
      ["toast toast--warning", "status"], ["toast toast--error", "alert"],
    ]);
  }],

  ["toast: info/success/warning закрываются сами через timeoutMs", () => {
    const { toaster, timers } = setup({ timeoutMs: 5000 });
    toaster.show({ tone: "success", text: "Готово" });
    timers.advance(4999);
    assertEqual(toaster.count(), 1);
    timers.advance(1);
    assertEqual(toaster.count(), 0);
    assertEqual(toastsIn(toaster).length, 0, "узел удалён из DOM");
  }],

  ["toast: ошибки сами не закрываются", () => {
    const { toaster, timers } = setup();
    toaster.show({ tone: "error", text: "Ошибка" });
    assertEqual(timers.pending, 0, "таймер для ошибки не ставится");
    timers.advance(60_000);
    assertEqual(toaster.count(), 1);
  }],

  ["toast: кнопка «×» с подписью закрывает уведомление", () => {
    const { toaster } = setup();
    toaster.show({ tone: "error", text: "Ошибка" });
    const close = byClass(toaster.region, "toast-close")[0];
    assertEqual(close.getAttribute("aria-label"), "Закрыть уведомление");
    assertEqual(close.getAttribute("type"), "button");
    close.dispatch("click", {});
    assertEqual(toaster.count(), 0);
  }],

  ["toast: Esc закрывает самое новое уведомление и не трогает чужие клавиши", () => {
    const { doc, toaster } = setup();
    toaster.show({ text: "первое" });
    toaster.show({ tone: "error", text: "второе" });
    let prevented = false;
    doc.dispatch("keydown", { key: "Escape", defaultPrevented: false, preventDefault: () => { prevented = true; } });
    assertEqual(toastsIn(toaster).map((t) => t.textContent.replace("×", "")), ["первое"]);
    assert(prevented, "Esc обработан");
    doc.dispatch("keydown", { key: "Enter", preventDefault: () => {} });
    doc.dispatch("keydown", { key: "Escape", defaultPrevented: true, preventDefault: () => {} });
    assertEqual(toaster.count(), 1, "Enter и уже обработанный Esc не закрывают");
  }],

  ["toast: пауза таймера при наведении и фокусе", () => {
    const { toaster, timers } = setup({ timeoutMs: 1000 });
    toaster.show({ text: "Подсказка" });
    const [toast] = toastsIn(toaster);
    timers.advance(900);
    toast.dispatch("mouseenter");
    timers.advance(5000);
    assertEqual(toaster.count(), 1, "под указателем не закрывается");
    toast.dispatch("mouseleave");
    timers.advance(999);
    assertEqual(toaster.count(), 1);
    toast.dispatch("focusin");
    timers.advance(5000);
    assertEqual(toaster.count(), 1, "с фокусом не закрывается");
    toast.dispatch("focusout");
    timers.advance(1000);
    assertEqual(toaster.count(), 0);
  }],

  ["toast: не больше max, лишними убираются старые не-ошибки", () => {
    const { toaster } = setup({ max: 3 });
    toaster.show({ tone: "error", text: "E1" });
    toaster.show({ text: "I1" });
    toaster.show({ text: "I2" });
    toaster.show({ text: "I3" });
    assertEqual(toastsIn(toaster).map((t) => byClass(t, "toast-text")[0].textContent), ["E1", "I2", "I3"]);
    toaster.show({ tone: "error", text: "E2" });
    toaster.show({ tone: "error", text: "E3" });
    toaster.show({ tone: "error", text: "E4" });
    assertEqual(toastsIn(toaster).map((t) => byClass(t, "toast-text")[0].textContent), ["E2", "E3", "E4"]);
  }],

  ["toast: dismiss / dismissAll и повторное закрытие", () => {
    const { toaster, timers } = setup();
    const id = toaster.show({ text: "a" });
    toaster.show({ text: "b" });
    assertEqual(toaster.dismiss(id), true);
    assertEqual(toaster.dismiss(id), false);
    toaster.dismissAll();
    assertEqual(toaster.count(), 0);
    assertEqual(timers.pending, 0, "таймеры закрытых уведомлений отменены");
  }],

  ["toast: неверные параметры", () => {
    const { toaster } = setup();
    assertThrows(() => toaster.show({ tone: "danger", text: "x" }), TypeError);
    assertThrows(() => toaster.show({ text: "" }), TypeError);
    assertThrows(() => toaster.show({ text: 42 }), TypeError);
    assertThrows(() => toaster.show({ text: "x", timeoutMs: 0 }), TypeError);
    assertThrows(() => createToaster({}), TypeError);
  }],
];
