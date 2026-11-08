// Запуск unit-тестов витрины storefront в Node.js 18+ (без сети, без зависимостей):
//   node frontend/tests/storefront/run-node.mjs
import { tests as domTests } from "./dom.tests.js";
import { tests as routerTests } from "./router.tests.js";
import { tests as storageTests } from "./storage.tests.js";
import { tests as buttonTests } from "./button.tests.js";
import { tests as toastTests } from "./toast.tests.js";
import { tests as statesTests } from "./states.tests.js";
import { tests as publishTests } from "./publish.tests.js";
import { tests as catalogModelTests } from "./catalog-model.tests.js";
import { tests as catalogSourceTests } from "./catalog-source.tests.js";
import { tests as productCardTests } from "./product-card.tests.js";
import { tests as catalogViewTests } from "./catalog-view.tests.js";
import { tests as headerTests } from "./header.tests.js";
import { tests as catalogSafetyTests } from "./catalog-safety.tests.js";

const suites = [
  ...domTests, ...routerTests, ...storageTests,
  ...buttonTests, ...toastTests, ...statesTests, ...publishTests,
  ...catalogModelTests, ...catalogSourceTests, ...productCardTests,
  ...catalogViewTests, ...headerTests, ...catalogSafetyTests,
];
let failed = 0;

for (const [name, fn] of suites) {
  try {
    await fn();
    console.log(`ok    ${name}`);
  } catch (error) {
    failed += 1;
    console.log(`FAIL  ${name}`);
    console.log(`      ${error.message}`);
  }
}

console.log(`\n${suites.length - failed} из ${suites.length} тестов пройдено`);
process.exitCode = failed ? 1 : 0;
