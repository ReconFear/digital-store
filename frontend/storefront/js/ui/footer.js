// Подвал в палитре Digital Store. Контакты и ссылки берутся только из brand.js.

import { hrefFor } from "../core/router.js";

export function createFooter({ dom, brand, year = new Date().getFullYear() }) {
  const { el } = dom;

  const navLinks = [
    el("a", { text: "Каталог", attrs: { href: hrefFor("catalog") } }),
    el("a", { text: "Корзина", attrs: { href: hrefFor("cart") } }),
    el("a", { text: "Мой заказ", attrs: { href: hrefFor("order") } }),
    ...brand.links.map((link) => el("a", { text: link.label, attrs: { href: link.href } })),
    ...brand.contacts.map((contact) => el("a", { text: contact.label, attrs: { href: contact.href } })),
  ];

  const node = el(
    "div",
    { className: "footer-container" },
    el(
      "div",
      {},
      el("p", { className: "footer-brand", text: `© ${year} ${brand.name}` }),
      el("p", { className: "footer-tagline", text: brand.footer.tagline }),
    ),
    el("nav", { className: "footer-nav", attrs: { "aria-label": "Ссылки в подвале" } }, navLinks),
  );

  return Object.freeze({ node });
}
