// Общие проверки для тестов витрины (тот же стиль, что в api-client.tests.js).

export function assert(condition, message) {
  if (!condition) throw new Error(message || "assertion failed");
}

export function assertEqual(actual, expected, message) {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a !== e) throw new Error(`${message || "не равно"}: получено ${a}, ожидалось ${e}`);
}

export function assertThrows(fn, ErrorType, message) {
  try {
    fn();
  } catch (error) {
    if (ErrorType && !(error instanceof ErrorType)) {
      throw new Error(`${message || "неверный тип ошибки"}: ${error}`);
    }
    return error;
  }
  throw new Error(message || "ожидалось исключение");
}
