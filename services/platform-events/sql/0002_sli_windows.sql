-- Upgrade an existing v1 database as the migration login. Never bootstrap again.
-- Upgrade readers to the v1/v2-compatible release before applying this migration;
-- old v1-only readers fail closed on v2. No business/Executor database is used.
BEGIN;
SET LOCAL ROLE bravo_platform_migrator;
SET LOCAL search_path = pg_catalog, bravo_platform;
LOCK TABLE bravo_platform.schema_version IN ACCESS EXCLUSIVE MODE;
DO $$ BEGIN
    IF (SELECT count(*) FROM bravo_platform.schema_version) <> 1
        OR NOT EXISTS (SELECT 1 FROM bravo_platform.schema_version WHERE version = 1)
    THEN RAISE EXCEPTION 'expected_storage_schema_v1'; END IF;
END $$;
CREATE TABLE bravo_platform.sli_windows (
    plan_id TEXT NOT NULL REFERENCES bravo_platform.plans(plan_id),
    start_us BIGINT NOT NULL, end_us BIGINT NOT NULL CHECK (end_us > start_us),
    document_json TEXT NOT NULL,
    PRIMARY KEY (plan_id, start_us)
);
CREATE INDEX sli_windows_latest ON bravo_platform.sli_windows (plan_id, end_us DESC);
GRANT SELECT, INSERT, DELETE ON bravo_platform.sli_windows TO bravo_platform_runtime;
ALTER TABLE bravo_platform.schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE bravo_platform.schema_version ADD CHECK (version IN (1, 2));
UPDATE bravo_platform.schema_version SET version = 2;
COMMIT;
