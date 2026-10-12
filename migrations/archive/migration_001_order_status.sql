BEGIN;

ALTER TABLE orders
ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'legacy'
CHECK (status IN ('pending', 'paid', 'cancelled', 'legacy'));

ALTER TABLE orders
ALTER COLUMN status SET DEFAULT 'pending';

COMMIT;