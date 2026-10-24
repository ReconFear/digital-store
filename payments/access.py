"""Секрет доступа к заказу (владение заказом без учётных записей покупателей).

* При создании заказа через API v2 сервер генерирует 256-битный случайный секрет
  (secrets.token_urlsafe(32), 43 символа) и возвращает его покупателю ОДИН раз.
* В базе хранится только SHA-256 секрета (order_access.token_sha256). Утечка базы
  не даёт готовых секретов; по хэшу секрет не восстановить.
* Посмотреть статус или начать оплату можно только с парой (id заказа, секрет).
  Неверный секрет и несуществующий заказ неотличимы (одинаковый ответ 404).
* Подбор: 2^256 вариантов + ограничение частоты запросов (http_security.py).
* Заказы API v1 и заказы, созданные до PR 3, секрета не имеют и через checkout
  не оплачиваются (см. PAYMENTS.md).
"""

import hashlib
import re
import secrets

from commerce.orders import create_order

TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


def generate_token():
    return secrets.token_urlsafe(32)


def token_sha256_hex(token):
    """SHA-256 секрета в hex или None, если строка не похожа на секрет (тогда — «не найдено»)."""
    if not isinstance(token, str) or not TOKEN_RE.match(token):
        return None
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_order_with_access(items, after_access=None):
    """Создаёт заказ v2 и секрет доступа к нему в одной транзакции. -> (order, token).

    after_access(cursor, order_id) — необязательный шаг в той же транзакции ПОСЛЕ выдачи секрета и до COMMIT
    (PR6: привязка заказа к аккаунту, accounts/order_link.py). Исключение в нём отменяет заказ вместе с секретом.
    """
    token = generate_token()

    def store_token(cursor, order_id):
        # Функция БД выдаёт секрет только заказу, создаваемому в этой же транзакции.
        cursor.execute("SELECT order_access_issue(%s, decode(%s, 'hex'))", (order_id, token_sha256_hex(token)))
        if after_access is not None:
            after_access(cursor, order_id)

    order = create_order(items, on_created=store_token)
    return order, token
