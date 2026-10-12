// Тестовая проверка POST /orders. Создаёт реальную запись заказа "pending" — без оплаты.
import { createApiClient } from "../../lib/digital-store-api.js";
import { API_BASE_URL } from "./config.js";

// allowOrderCreation включён только здесь, на отдельной тестовой странице.
const api = createApiClient({ baseUrl: API_BASE_URL, allowOrderCreation: true });

const form = document.getElementById("order-form");
const input = document.getElementById("product-id");
const confirm = document.getElementById("confirm");
const submit = document.getElementById("submit");
const statusBox = document.getElementById("status");

function line(text, className) {
  const p = document.createElement("p");
  p.textContent = text;
  if (className) p.className = className;
  return p;
}

confirm.addEventListener("change", () => {
  submit.disabled = !confirm.checked;
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!confirm.checked) return;

  const productId = Number(input.value);
  submit.disabled = true;
  statusBox.replaceChildren(line("Отправляем запрос…", "loading"));

  try {
    const { message, order } = await api.createOrder(productId);
    const box = document.createElement("div");
    box.className = "message";
    box.append(
      line(`Ответ backend: ${message}`),
      line(`Заказ №${order.id}: товар ${order.product_id}, цена ${order.price}, статус «${order.status}».`),
      line("Это не покупка: оплата не проводилась, товар не выдан.", "note"),
    );
    statusBox.replaceChildren(box);
  } catch (error) {
    const box = document.createElement("div");
    box.className = "message message--error";
    box.append(line(error.userMessage || "Ошибка"), line(error.message, "note"));
    statusBox.replaceChildren(box);
  } finally {
    // Подтверждение сбрасывается после каждой попытки — повторный заказ только осознанно.
    confirm.checked = false;
    submit.disabled = true;
  }
});
