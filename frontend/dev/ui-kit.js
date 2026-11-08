// ТОЛЬКО ДЛЯ РАЗРАБОТКИ: образцы компонентов витрины Digital Store.
// Не публикуется (см. комментарий в ui-kit.html). Сетевых запросов, товаров, цен, заказов и оплат нет:
// все «загрузки» здесь — имитация таймером.

import { createDom } from "../storefront/js/core/dom.js";
import { createButton } from "../storefront/js/ui/button.js";
import { createStates } from "../storefront/js/ui/states.js";
import { createToaster } from "../storefront/js/ui/toast.js";

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]"]);
const dom = createDom(document);
const { el } = dom;
const main = document.getElementById("main");

const wait = (ms) => new Promise((resolve) => { setTimeout(resolve, ms); });

function section(title, ...children) {
  return el("section", { className: "uikit-section" }, el("h2", { className: "page-title", text: title }), ...children);
}

function row(...children) {
  return el("div", { className: "state-actions" }, ...children);
}

function render() {
  const states = createStates(dom);
  const toaster = createToaster({ dom, root: document.body, keyTarget: document });

  const buttons = section(
    "Кнопки",
    row(
      createButton(dom, { label: "Основная", onClick: () => wait(1500) }).node,
      createButton(dom, { label: "Вторичная", variant: "secondary", onClick: () => wait(1500) }).node,
      createButton(dom, { label: "Неактивная", disabled: true }).node,
      createButton(dom, { label: "Неактивная", variant: "secondary", disabled: true }).node,
    ),
    row(
      (() => { const b = createButton(dom, { label: "Загрузка" }); b.setLoading(true); return b.node; })(),
      (() => { const b = createButton(dom, { label: "Загрузка", variant: "secondary" }); b.setLoading(true); return b.node; })(),
    ),
    el("p", { className: "message-text", text: "Нажатие на первые две кнопки имитирует действие на 1,5 с: повторные нажатия в это время не срабатывают." }),
  );

  const chips = section(
    "Чипы",
    el(
      "div",
      { className: "categories", attrs: { role: "group", "aria-label": "Образцы чипов" } },
      el("button", { className: "category active", text: "Активный", attrs: { type: "button", "aria-pressed": "true" } }),
      el("button", { className: "category", text: "Обычный", attrs: { type: "button", "aria-pressed": "false" } }),
      el("button", { className: "category", text: "Неактивный", attrs: { type: "button", disabled: true } }),
    ),
  );

  const messages = section(
    "Сообщения",
    states.message({ tone: "info", title: "Информация", text: "Нейтральное сообщение для покупателя." }),
    states.message({ tone: "success", title: "Готово", text: "Действие выполнено." }),
    states.message({ tone: "warning", title: "Обратите внимание", text: "Предупреждение без ошибки." }),
    states.message({ tone: "error", title: "Ошибка", text: "Что-то пошло не так.", actions: [{ label: "На главную", href: "../storefront/" }] }),
    states.notice({ text: "Блок «Скоро» из T1 — без изменений." }),
  );

  const toastSection = section(
    "Toast-уведомления",
    row(
      ...["info", "success", "warning", "error"].map((tone) =>
        createButton(dom, {
          label: `Показать ${tone}`,
          variant: "secondary",
          onClick: () => {
            toaster.show({
              tone,
              title: tone === "error" ? "Ошибка" : undefined,
              text: tone === "error"
                ? "Ошибки не закрываются сами — только кнопкой × или клавишей Esc."
                : "Закроется само через 5 секунд (пауза при наведении и фокусе).",
            });
          },
        }).node),
      createButton(dom, { label: "Закрыть все", variant: "secondary", onClick: () => toaster.dismissAll() }).node,
    ),
  );

  // Состояние сетевой ошибки: первая попытка «не удаётся», вторая показывает пустой результат.
  const retryHost = el("div");
  let attempts = 0;
  function showNetworkError() {
    retryHost.replaceChildren(states.networkError({
      onRetry: async () => {
        attempts += 1;
        await wait(1200);
        if (attempts % 2 === 1) {
          showNetworkError();
        } else {
          retryHost.replaceChildren(states.empty({ title: "Ничего не найдено", text: "Пустой результат после повтора." }));
        }
      },
    }));
  }
  showNetworkError();

  const stateSection = section(
    "Состояния",
    states.loading("Загрузка каталога…"),
    states.empty({ title: "Пусто", text: "Здесь пока ничего нет.", actions: [{ label: "К витрине", href: "../storefront/" }] }),
    states.error({ text: "Ошибка экрана с повтором (T1).", onRetry: () => wait(800) }),
    retryHost,
  );

  main.replaceChildren(
    el("h1", { className: "page-title", text: "UI Kit Digital Store — только для разработки" }),
    states.message({ tone: "warning", text: "Служебная страница. Не публикуется и не связана с магазином." }),
    buttons,
    chips,
    messages,
    toastSection,
    stateSection,
  );
}

if (LOCAL_HOSTS.has(location.hostname)) {
  render();
} else {
  main.replaceChildren(el("p", { className: "state", text: "Эта страница доступна только при локальной разработке." }));
}
