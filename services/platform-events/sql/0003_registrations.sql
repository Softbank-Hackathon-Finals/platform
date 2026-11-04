-- Deploy readers that accept v3 (including window intake) before upgrading.
BEGIN;
SET LOCAL ROLE bravo_platform_migrator;
SET LOCAL search_path = pg_catalog, bravo_platform;
LOCK TABLE bravo_platform.schema_version IN ACCESS EXCLUSIVE MODE;
DO $$ BEGIN
    IF (SELECT count(*) FROM bravo_platform.schema_version) <> 1
        OR NOT EXISTS (SELECT 1 FROM bravo_platform.schema_version WHERE version = 2)
    THEN RAISE EXCEPTION 'expected_storage_schema_v2'; END IF;
END $$;
CREATE TABLE bravo_platform.registrations (
    tenant_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('environment','workload','plan')),
    object_id TEXT NOT NULL, command_key TEXT NOT NULL, request_json TEXT NOT NULL, receipt_json TEXT NOT NULL,
    PRIMARY KEY (tenant_id, kind, object_id), UNIQUE(tenant_id, kind, command_key)
);
GRANT SELECT, INSERT ON bravo_platform.registrations TO bravo_platform_runtime;
ALTER TABLE bravo_platform.schema_version DROP CONSTRAINT schema_version_version_check;
ALTER TABLE bravo_platform.schema_version ADD CHECK (version IN (1, 2, 3));
UPDATE bravo_platform.schema_version SET version = 3;
COMMIT;
