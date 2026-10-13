"""Платёжный обработчик: очередь уведомлений и сверка с провайдером.

Запуск (под ролью digital_store_worker, см. DB_ROLES.md):
    python -m payments.worker --once          # один проход (для планировщика задач)
    python -m payments.worker --interval 10   # постоянно, проход каждые 10 секунд

Безопасен при перезапуске и при нескольких экземплярах: события захватываются через
FOR UPDATE SKIP LOCKED с арендой, учёт оплаты идемпотентен (payment_record_provider_state).
Ошибки базы данных не подавляются: процесс завершается с ненулевым кодом.
"""

import argparse
import logging
import sys
import time

from payments import build_payment_service


def main(argv=None):
    parser = argparse.ArgumentParser(description="Платёжный обработчик Digital Store")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="один проход и выход")
    group.add_argument("--interval", type=float, help="пауза между проходами, секунд (1..3600)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    service = build_payment_service()
    if not service.settings.enabled:
        print("Платежи выключены (PAYMENTS_ENABLED=false): обрабатывать нечего.")
        return 0
    if args.once:
        print(service.reconcile())
        return 0
    if not 1 <= args.interval <= 3600:
        parser.error("--interval: от 1 до 3600 секунд")
    while True:
        service.reconcile()
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
