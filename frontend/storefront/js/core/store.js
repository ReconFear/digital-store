// Простое хранилище состояния страницы (в памяти) с подпиской на изменения.

export function createStore(initial = {}) {
  if (initial === null || typeof initial !== "object" || Array.isArray(initial)) {
    throw new TypeError("createStore: начальное состояние должно быть объектом");
  }
  let state = Object.freeze({ ...initial });
  const listeners = new Set();

  return Object.freeze({
    get() {
      return state;
    },

    /** set({ поле: значение }) или set((state) => ({ поле: значение })) — частичное обновление. */
    set(patch) {
      const next = typeof patch === "function" ? patch(state) : patch;
      if (next === null || typeof next !== "object" || Array.isArray(next)) {
        throw new TypeError("store.set: ожидается объект изменений");
      }
      const previous = state;
      state = Object.freeze({ ...state, ...next });
      for (const listener of [...listeners]) {
        try {
          listener(state, previous);
        } catch {
          // Ошибка одного подписчика не должна мешать остальным.
        }
      }
      return state;
    },

    /** Возвращает функцию отписки. */
    subscribe(listener) {
      if (typeof listener !== "function") throw new TypeError("store.subscribe: нужна функция");
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  });
}
