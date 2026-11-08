// Проверки границы публикации: служебная страница UI Kit (frontend/dev/) не должна
// попасть в магазин. Публикуются только frontend/storefront/ и frontend/lib/.

import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { assert, assertEqual } from "./assert.js";

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STOREFRONT = join(FRONTEND, "storefront");
const DEV = join(FRONTEND, "dev");

function filesIn(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? filesIn(path) : [path];
  });
}

export const tests = [
  ["publish: UI Kit лежит вне frontend/storefront/", () => {
    const inside = filesIn(STOREFRONT).filter((path) => /ui-kit/i.test(path));
    assertEqual(inside, [], "в папке магазина не должно быть файлов UI Kit");
    assert(statSync(join(DEV, "ui-kit.html")).isFile());
  }],

  ["publish: магазин не ссылается на служебные страницы", () => {
    for (const path of filesIn(STOREFRONT)) {
      const text = readFileSync(path, "utf8");
      assert(!/ui-kit|\.\.\/dev\/|\/dev\//i.test(text), `ссылка на dev в ${relative(FRONTEND, path)}`);
    }
  }],

  ["publish: UI Kit помечен как служебный и работает только локально", () => {
    const html = readFileSync(join(DEV, "ui-kit.html"), "utf8");
    const js = readFileSync(join(DEV, "ui-kit.js"), "utf8");
    assert(/<meta name="robots" content="noindex, nofollow">/.test(html), "noindex");
    assert(/connect-src 'none'/.test(html), "страница образцов не делает сетевых запросов");
    assert(/LOCAL_HOSTS\.has\(location\.hostname\)/.test(js), "проверка localhost перед отрисовкой");
    assert(!/digital-store-api|createApiClient|fetch\(/.test(js), "UI Kit не обращается к API");
  }],
];
