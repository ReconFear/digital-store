// Статические проверки T3: витрина только читает каталог. Нет заказов, оплаты,
// выдачи ключей, прямых сетевых вызовов, выдуманных товаров, цен и изображений.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { assert, assertEqual } from "./assert.js";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STOREFRONT = join(FRONTEND, "storefront");

function filesIn(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? filesIn(path) : [path];
  });
}

const scripts = () => filesIn(STOREFRONT).filter((path) => path.endsWith(".js"));
const name = (path) => relative(FRONTEND, path).replace(/\\/g, "/");

export const tests = [
  ["catalog-safety: витрина не вызывает заказы, оплату, статус и выдачу", () => {
    const forbidden = /createOrder|checkoutV2|getOrderStatusV2|getDeliveryV2|allowOrderCreation|\/delivery|\/checkout/;
    const offenders = scripts().filter((path) => forbidden.test(readFileSync(path, "utf8"))).map(name);
    assertEqual(offenders, []);
  }],

  ["catalog-safety: сеть — только через общий клиент, и только в js/core/api.js", () => {
    const importers = scripts()
      .filter((path) => /digital-store-api\.js/.test(readFileSync(path, "utf8")))
      .map(name);
    assertEqual(importers, ["storefront/js/core/api.js"]);
    const direct = scripts()
      .filter((path) => /\bfetch\s*\(|XMLHttpRequest|WebSocket|sendBeacon|EventSource/.test(readFileSync(path, "utf8")))
      .map(name);
    assertEqual(direct, []);
    const api = readFileSync(join(STOREFRONT, "js", "core", "api.js"), "utf8");
    assert(api.includes("getProductsV2"), "каталог читается через getProductsV2");
    assert(!/method\s*:/.test(api), "витрина не задаёт HTTP-методы сама");
  }],

  ["catalog-safety: в витрине нет выдуманных товаров, цен и изображений", () => {
    for (const path of filesIn(STOREFRONT)) {
      const text = readFileSync(path, "utf8");
      assert(!/amount_minor\s*:\s*\d/.test(text), `цена-литерал в ${name(path)}`);
      assert(!/\.(png|jpe?g|gif|webp|avif|svg)\b/i.test(text), `изображение в ${name(path)}`);
      assert(!/el\(\s*"img"/.test(text), `элемент img в ${name(path)}`);
      assert(!/description|image_url|category_id/.test(text), `поле вне контракта в ${name(path)}`);
    }
  }],
];
