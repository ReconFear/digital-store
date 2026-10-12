import { runTests } from "./api-client.tests.js";

const list = document.getElementById("results");
const summary = document.getElementById("summary");

const { total, failed } = await runTests((name, error) => {
  const item = document.createElement("li");
  item.className = error ? "fail" : "ok";
  item.textContent = `${error ? "✗" : "✓"} ${name}`;
  if (error) {
    const detail = document.createElement("pre");
    detail.textContent = error.message;
    item.append(detail);
  }
  list.append(item);
});

summary.textContent = `${total - failed} из ${total} тестов пройдено`;
summary.className = failed ? "fail" : "ok";
document.documentElement.dataset.testsDone = failed ? "failed" : "passed";
