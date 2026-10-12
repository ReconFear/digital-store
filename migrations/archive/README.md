# Архив миграций

Файлы здесь применялись вручную до появления `migrate.py` и сохранены для истории.
`migrate.py` их **не запускает** (подпапки игнорируются).

- `migration_001_order_status.sql` — применён вручную к `digital_store` и `digital_store_test`.
  Его действующий эквивалент без `BEGIN/COMMIT` — `migrations/0001_order_status.sql`.
