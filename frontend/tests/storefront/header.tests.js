// Тесты поиска в шапке (js/ui/header.js): включение по features.search, задержка ввода,
// Enter, ограничение длины, синхронизация поля. Поиск не обращается к API.

import { createDom } from "../../storefront/js/core/dom.js";
import { createHeader, SEARCH_DEBOUNCE_MS, SEARCH_MAX_LENGTH } from "../../storefront/js/ui/header.js";
import { BRAND } from "../../storefront/brand/brand.js";
import { allElements, createFakeDocument, fakeTimers } from "./fake-dom.js";
import { assert, assertEqual } from "./assert.js";

const config = (search) => ({ features: { search } });

function setup({ search = true } = {}) {
  const timers = fakeTimers();
  const inputs = [];
  const submits = [];
  const header = createHeader({
    dom: createDom(createFakeDocument()),
    brand: BRAND,
    config: config(search),
    search: {
      onInput: (query) => inputs.push(query),
      onSubmit: (query) => submits.push(query),
      schedule: timers.schedule,
      cancel: timers.cancel,
    },
  });
  const input = allElements(header.node).find((node) => node.localName === "input");
  const form = allElements(header.node).find((node) => node.localName === "form");
  return { header, input, form, timers, inputs, submits };
}

function type(input, value) {
  input.value = value;
  input.dispatch("input", {});
}

export const tests = [
  ["header: поиск включён — «Поиск товаров...», ограничение длины", () => {
    const { input } = setup();
    assertEqual(input.getAttribute("placeholder"), "Поиск товаров...");
    assertEqual(input.hasAttribute("disabled"), false);
    assertEqual(input.getAttribute("maxlength"), String(SEARCH_MAX_LENGTH));
    assertEqual(input.getAttribute("type"), "search");
  }],

  ["header: features.search выключен — поле неактивно, как в T1", () => {
    const { input, form, inputs, submits } = setup({ search: false });
    assert(input.hasAttribute("disabled"));
    assertEqual(input.getAttribute("aria-describedby"), "site-search-hint");
    let prevented = false;
    form.dispatch("submit", { preventDefault: () => { prevented = true; } });
    assert(prevented, "форма никогда не отправляется");
    assertEqual([inputs.length, submits.length], [0, 0]);
  }],

  ["header: ввод передаётся с задержкой, быстрый набор — одно обновление", () => {
    const { input, timers, inputs } = setup();
    type(input, "к");
    type(input, "кл");
    type(input, "клю");
    assertEqual(inputs, [], "сразу ничего не фильтруется");
    timers.advance(SEARCH_DEBOUNCE_MS - 1);
    assertEqual(inputs, []);
    timers.advance(1);
    assertEqual(inputs, ["клю"]);
    assertEqual(timers.pending, 0);
  }],

  ["header: Enter — сразу запрос и переход, отложенный ввод отменяется", () => {
    const { input, form, timers, inputs, submits } = setup();
    type(input, "ключ");
    let prevented = false;
    form.dispatch("submit", { preventDefault: () => { prevented = true; } });
    assert(prevented, "страница не перезагружается");
    assertEqual(inputs, ["ключ"]);
    assertEqual(submits, ["ключ"]);
    timers.advance(1000);
    assertEqual(inputs, ["ключ"], "без повторного обновления");
  }],

  ["header: слишком длинный запрос обрезается до 100 символов", () => {
    const { input, form, inputs } = setup();
    input.value = "я".repeat(150);
    form.dispatch("submit", { preventDefault() {} });
    assertEqual(inputs[0].length, SEARCH_MAX_LENGTH);
  }],

  ["header: setSearchValue обновляет поле, но не мешает набору", () => {
    const { header, input, timers } = setup();
    header.setSearchValue("ключ");
    assertEqual(input.value, "ключ");
    type(input, "клю");
    header.setSearchValue("");
    assertEqual(input.value, "клю", "пока набор не обработан, поле не перезаписывается");
    timers.advance(SEARCH_DEBOUNCE_MS);
    header.setSearchValue("");
    assertEqual(input.value, "", "после «Сбросить фильтры» поле очищается");
  }],
];
