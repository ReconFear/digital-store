"""Worker исполнения оплаченных заказов.

Запуск (под ролью <prefix>_worker, см. DB_ROLES.md и DIGITAL_KEYS.md):
    python -m commerce.fulfillment_worker --once
    python -m commerce.fulfillment_worker --interval 10

Модули включаются явно: FULFILLMENT_MODULES=digital_keys (через запятую). По умолчанию пусто —
ничего не выдаётся, задачи остаются "pending" (поведение PR2/PR3).

Worker НЕ получает секретов шифрования: модуль digital_keys назначает зашифрованные ключи,
а расшифровывает их только веб-процесс при показе покупателю. Переменные DIGITAL_KEYS_* в
окружении worker не нужны; если они там есть, worker отказывается запускаться, чтобы секрет
не оказался там, где он не нужен.
"""

import argparse
import logging
import os
import sys
import time

from commerce import fulfillment

AVAILABLE_MODULES = {
    "digital_keys": "commerce.digital_keys.handler",
}
SECRET_VARIABLES = ("DIGITAL_KEYS_ENCRYPTION_KEYS", "DIGITAL_KEYS_FINGERPRINT_KEY", "DIGITAL_KEYS_ACTIVE_KEY",
                    "DIGITAL_KEYS_SECRETS_FILE")


class ConfigError(Exception):
    pass


def configured_modules(env=None):
    env = os.environ if env is None else env
    names = [name.strip() for name in env.get("FULFILLMENT_MODULES", "").split(",") if name.strip()]
    unknown = [name for name in names if name not in AVAILABLE_MODULES]
    if unknown:
        raise ConfigError(f"FULFILLMENT_MODULES: неизвестные модули {unknown}; доступны {sorted(AVAILABLE_MODULES)}")
    if len(set(names)) != len(names):
        raise ConfigError("FULFILLMENT_MODULES: модуль указан дважды")
    return names


def check_no_secrets(env=None):
    env = os.environ if env is None else env
    present = [name for name in SECRET_VARIABLES if env.get(name)]
    if present:
        raise ConfigError(f"worker не должен получать секреты шифрования; уберите из его окружения: {present}")


def install(names):
    import importlib

    for name in names:
        module = importlib.import_module(AVAILABLE_MODULES[name])
        if name not in fulfillment.registered_types():
            fulfillment.register_handler(module.handler())


def main(argv=None, env=None):
    parser = argparse.ArgumentParser(description="Исполнение оплаченных заказов Digital Store")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--once", action="store_true", help="один проход и выход")
    group.add_argument("--interval", type=float, help="пауза между проходами, секунд (1..3600)")
    parser.add_argument("--limit", type=int, default=50, help="задач за проход (1..1000)")
    args = parser.parse_args(argv)
    if args.interval is not None and not 1 <= args.interval <= 3600:
        parser.error("--interval: от 1 до 3600 секунд")
    if not 1 <= args.limit <= 1000:
        parser.error("--limit: от 1 до 1000")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        check_no_secrets(env)
        names = configured_modules(env)
    except ConfigError as exc:
        print(f"Ошибка настройки: {exc}", file=sys.stderr)
        return 2
    if not names:
        print("FULFILLMENT_MODULES пуст: модулей выдачи нет, задачи остаются в ожидании.")
        return 0
    install(names)
    log = logging.getLogger("fulfillment")
    while True:
        summary = fulfillment.process_pending_fulfillments(limit=args.limit)
        log.info("проход: %s", summary)
        if args.once:
            print(summary)
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
