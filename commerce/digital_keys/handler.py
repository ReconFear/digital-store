"""Модуль исполнения digital_keys для worker. Секретов шифрования НЕ получает и не требует:
он только назначает задаче свободные зашифрованные ключи в базе (digital_keys_assign).

Транзакцией не управляет: работает внутри транзакции и SAVEPOINT воркера (commerce/fulfillment.py).
Порядок блокировок соблюдает функция базы: задача -> платёж -> advisory(товар) -> ключи.
"""

import json

from commerce.fulfillment import FulfillmentError, FulfillmentResult


class DigitalKeysHandler:
    fulfillment_type = "digital_keys"

    def fulfill(self, conn, task):
        with conn.cursor() as cursor:
            cursor.execute("SELECT digital_keys_assign(%s)", (task.id,))
            raw = cursor.fetchone()[0]
        result = json.loads(raw) if isinstance(raw, str) else raw
        outcome = result.get("result")
        if outcome in ("assigned", "already_assigned"):
            return FulfillmentResult.fulfilled(f"Выдано ключей: {int(result['count'])}")
        if outcome == "insufficient":
            return FulfillmentResult.requires_action(
                f"Не хватает ключей: нужно {int(result['need'])}, свободно {int(result['available'])}")
        if outcome == "refund_hold":
            return FulfillmentResult.requires_action("Выдача остановлена: по оплате заказа запрошен возврат")
        # not_paid / not_pending: задачу захватили вне условий выдачи — повтор позже.
        raise FulfillmentError(f"выдача невозможна: {outcome}")


def handler():
    return DigitalKeysHandler()
