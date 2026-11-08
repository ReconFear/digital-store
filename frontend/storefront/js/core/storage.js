// Защищённые обёртки браузерного хранилища.
// Хранилище может отсутствовать, бросать исключения (приватный режим, запрет сайта),
// быть переполнено или содержать чужие/повреждённые данные — витрина от этого не падает.
// ВАЖНО: секреты заказа (access_token) и цифровые ключи сюда не пишутся без отдельного решения.

const KEY_RE = /^[a-z0-9][a-z0-9:_-]{0,63}$/i;

/**
 * @param {object} options
 * @param {() => Storage} options.backend функция, возвращающая хранилище (обращение может бросить)
 * @param {string} options.prefix префикс ключей, например "ds:digital-store:"
 * @param {number} [options.version] версия формата; записи другой версии игнорируются
 * @param {() => number} [options.now] текущее время, мс (для тестов)
 */
export function createStorage({ backend, prefix, version = 1, now = () => Date.now() }) {
  if (typeof backend !== "function") throw new TypeError("createStorage: backend должен быть функцией");
  if (typeof prefix !== "string" || !prefix) throw new TypeError("createStorage: нужен prefix");

  function fullKey(key) {
    if (typeof key !== "string" || !KEY_RE.test(key)) {
      throw new TypeError(`storage: недопустимый ключ ${String(key)}`);
    }
    return prefix + key;
  }

  function store() {
    try {
      const value = backend();
      return value && typeof value.getItem === "function" ? value : null;
    } catch {
      return null;
    }
  }

  function removeRaw(name) {
    try {
      store()?.removeItem(name);
    } catch {
      // Удалить не удалось — запись всё равно будет проигнорирована при чтении.
    }
  }

  return Object.freeze({
    /** Доступно ли хранилище для записи прямо сейчас. */
    isAvailable() {
      const target = store();
      if (!target) return false;
      const probe = `${prefix}__probe__`;
      try {
        target.setItem(probe, "1");
        target.removeItem(probe);
        return true;
      } catch {
        return false;
      }
    },

    /**
     * Читает значение. fallback возвращается, если записи нет, она повреждена, другой версии,
     * истекла или не прошла validate(data).
     */
    readJson(key, { validate = () => true, fallback = null } = {}) {
      const name = fullKey(key);
      const target = store();
      if (!target) return fallback;
      let raw;
      try {
        raw = target.getItem(name);
      } catch {
        return fallback;
      }
      if (raw === null || raw === undefined) return fallback;

      let envelope;
      try {
        envelope = JSON.parse(raw);
      } catch {
        removeRaw(name);
        return fallback;
      }
      const valid =
        envelope !== null &&
        typeof envelope === "object" &&
        envelope.v === version &&
        (envelope.exp === null || Number.isFinite(envelope.exp)) &&
        "data" in envelope;
      if (!valid) {
        removeRaw(name);
        return fallback;
      }
      if (envelope.exp !== null && envelope.exp <= now()) {
        removeRaw(name);
        return fallback;
      }
      let accepted = false;
      try {
        accepted = validate(envelope.data) === true;
      } catch {
        accepted = false;
      }
      if (!accepted) {
        removeRaw(name);
        return fallback;
      }
      return envelope.data;
    },

    /** Записывает значение. Возвращает true при успехе, false — если записать нельзя. */
    writeJson(key, data, { ttlMs = null } = {}) {
      const name = fullKey(key);
      if (ttlMs !== null && !(Number.isFinite(ttlMs) && ttlMs > 0)) {
        throw new TypeError("storage: ttlMs должен быть положительным числом или null");
      }
      if (data === undefined) return false;
      let text;
      try {
        text = JSON.stringify({ v: version, exp: ttlMs === null ? null : now() + ttlMs, data });
      } catch {
        return false;
      }
      const target = store();
      if (!target) return false;
      try {
        target.setItem(name, text);
        return true;
      } catch {
        return false;
      }
    },

    remove(key) {
      removeRaw(fullKey(key));
    },
  });
}

/** localStorage браузера; обращение к нему может бросить — createStorage это перехватывает. */
export function browserLocalStorage() {
  return globalThis.localStorage;
}
