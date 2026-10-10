BEGIN;

-- Running upgrade a101_cara_manager -> a102_business_master

ALTER TABLE cara_deliveries ADD COLUMN booking_session JSONB;

ALTER TABLE users ADD COLUMN is_business_master BOOLEAN DEFAULT false NOT NULL;

ALTER TABLE users ADD CONSTRAINT ck_users_business_master_scope CHECK (NOT is_business_master OR (role = 'spa_admin' AND tenant_id IS NOT NULL));

UPDATE alembic_version SET version_num='a102_business_master' WHERE alembic_version.version_num = 'a101_cara_manager';

COMMIT;

