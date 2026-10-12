// Запуск тестов JS-клиента в Node.js 18+ (необязательно — те же тесты есть в браузере):
//   node frontend/tests/run-node.mjs
import { runTests } from "./api-client.tests.js";

const { total, failed } = await runTests((name, error) => {
  console.log(`${error ? "FAIL" : "ok  "}  ${name}`);
  if (error) console.log(`      ${error.message}`);
});

console.log(`\n${total - failed} из ${total} тестов пройдено`);
process.exitCode = failed ? 1 : 0;
