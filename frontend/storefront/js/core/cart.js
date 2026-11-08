
import { createStore } from "./store.js";

const MAX_ITEMS = 50;
const MAX_QUANTITY = 1000;

export function createCart() {
  const store = createStore({ items: [] });

  function add(productId) {
    if (!Number.isSafeInteger(productId) || productId < 1) return false;

    const items = store.get().items;
    const existing = items.find((item) => item.product_id === productId);

    if (existing) {
      if (existing.quantity >= MAX_QUANTITY) return false;

      store.set({
        items: items.map((item) =>
          item.product_id === productId
            ? { ...item, quantity: item.quantity + 1 }
            : item
        ),
      });
    } else {
      if (items.length >= MAX_ITEMS) return false;

      store.set({
        items: [...items, { product_id: productId, quantity: 1 }],
      });
    }

    return true;
  }

  function remove(productId) {
    store.set({
      items: store.get().items.filter(
        (item) => item.product_id !== productId
      ),
    });
  }

  function setQuantity(productId, quantity) {
    if (!Number.isSafeInteger(quantity) || quantity < 0 || quantity > MAX_QUANTITY) {
      return false;
    }

    const items = store.get().items;
    if (!items.some((item) => item.product_id === productId)) return false;

    if (quantity === 0) {
      remove(productId);
    } else {
      store.set({
        items: items.map((item) =>
          item.product_id === productId ? { ...item, quantity } : item
        ),
      });
    }

    return true;
  }

  return Object.freeze({
    get: () => store.get(),
    subscribe: (listener) => store.subscribe(listener),
    add,
    remove,
    setQuantity,
    clear: () => store.set({ items: [] }),
  });
}
