"""Получение ключей покупателем: POST /api/v2/orders/{order_id}/delivery (веб-процесс, роль app).

1. Секрет заказа -> SHA-256 (payments/access.py). Неверный формат -> 404, как в /status.
2. order_delivery_view(id, sha256) — SECURITY DEFINER: заказ только по паре (id, секрет); шифротексты
   только у оплаченного заказа и выполненной задачи digital_keys; запись digital_keys.revealed.
3. В той же транзакции: проверочные значения версий ключей шифрования из реестра сверяются с
   секретами процесса; ключи расшифровываются в памяти (AES-256-GCM с AAD).
4. Любая ошибка настройки или расшифровки -> откат (в журнале показа нет записи о выдаче) и
   DeliveryError delivery_unavailable (503) без частичных данных. В лог — только номер заказа и
   UUID записи ключа, без значений.
"""

import json
import logging

import database
from commerce.digital_keys.crypto import DecryptionError, Keyring, KeyringError
from payments.access import token_sha256_hex

log = logging.getLogger("digital_keys.delivery")

MAX_ORDER_ID = 2_147_483_647


class DeliveryError(Exception):
    def __init__(self, code, http_status, message):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.http_status = http_status
        self.message = message

    def as_detail(self):
        return {"code": self.code, "message": self.message}


NOT_FOUND = ("order_not_found", 404, "Заказ не найден.")
UNAVAILABLE = ("delivery_unavailable", 503, "Выдача временно недоступна. Повторите попытку позже.")


def _jsonb(value):
    return json.loads(value) if isinstance(value, str) else value


def delivery_status(order_status, fulfillment_type, task_status):
    """Публичный статус позиции (FRONTEND_DELIVERY_API.md). Причины задержки не раскрываются."""
    if order_status == "pending":
        return "awaiting_payment"
    if order_status == "requires_action":
        return "delayed"
    if order_status != "paid" or task_status is None:
        return "not_applicable"
    if task_status == "cancelled":
        return "cancelled"
    if fulfillment_type != "digital_keys":
        return "manual"
    if task_status in ("pending", "in_progress"):
        return "processing"
    if task_status == "fulfilled":
        return "delivered"
    return "delayed"   # requires_action, failed


def _decrypt_items(cursor, view, keyring_loader, order_id):
    """Расшифровывает ключи позиций. Возвращает {order_item_id: [ключи]} или бросает DeliveryError."""
    needed = [item for item in view["items"] if isinstance(item.get("keys"), list) and item["keys"]]
    if not needed:
        return {}
    try:
        keyring = keyring_loader()
        cursor.execute("SELECT encryption_key_check_values()")
        registry = _jsonb(cursor.fetchone()[0]) or {}
        versions = sorted({key["enc_key_id"] for item in needed for key in item["keys"]})
        keyring.verify_against(registry, [f"encryption:{key_id}" for key_id in versions])
    except KeyringError as exc:
        log.error("выдача заказа %s невозможна: %s", order_id, exc)
        raise DeliveryError(*UNAVAILABLE) from None
    out = {}
    for item in needed:
        codes = []
        for key in item["keys"]:
            try:
                codes.append(keyring.decrypt(int(key["product_id"]), key["public_id"], key["enc_key_id"],
                                             bytes.fromhex(key["nonce"]), bytes.fromhex(key["ciphertext"])))
            except (DecryptionError, ValueError, KeyError, TypeError):
                log.error("выдача заказа %s: запись ключа %s не расшифровывается", order_id, key.get("public_id"))
                raise DeliveryError(*UNAVAILABLE) from None
        out[item["order_item_id"]] = codes
    return out


def default_keyring():
    return Keyring.from_env()


def get_delivery(order_id, access_token, keyring_loader=default_keyring):
    token_hex = token_sha256_hex(access_token)
    if token_hex is None or type(order_id) is not int or not 0 < order_id <= MAX_ORDER_ID:
        raise DeliveryError(*NOT_FOUND)
    conn = database.get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT order_delivery_view(%s, decode(%s, 'hex'))", (order_id, token_hex))
            raw = cursor.fetchone()[0]
            if raw is None:
                conn.rollback()
                raise DeliveryError(*NOT_FOUND)
            view = _jsonb(raw)
            codes = _decrypt_items(cursor, view, keyring_loader, order_id)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    items = []
    for item in view["items"]:
        status = delivery_status(view["order_status"], item["fulfillment_type"], item.get("fulfillment_status"))
        public = {
            "order_item_id": item["order_item_id"],
            "product_id": item["product_id"],
            "name": item["name"],
            "fulfillment_type": item["fulfillment_type"],
            "quantity": item["quantity"],
            "delivery_status": status,
        }
        if status == "delivered":
            keys = codes.get(item["order_item_id"], [])
            if len(keys) != item["quantity"]:
                log.error("выдача заказа %s: у позиции %s ключей %s из %s", order_id, item["order_item_id"],
                          len(keys), item["quantity"])
                raise DeliveryError(*UNAVAILABLE)
            public["digital_keys"] = keys
        items.append(public)
    return {"order_id": view["order_id"], "order_status": view["order_status"], "items": items}
