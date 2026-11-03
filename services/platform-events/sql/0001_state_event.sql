-- Run once with membership in the migration role, never with the runtime login.
BEGIN;
SET LOCAL ROLE bravo_platform_migrator;
SET LOCAL search_path = pg_catalog, bravo_platform;
CREATE TABLE bravo_platform.schema_version (version INTEGER PRIMARY KEY CHECK (version = 1));
CREATE TABLE bravo_platform.streams (
    plan_id TEXT PRIMARY KEY, next_sequence BIGINT NOT NULL DEFAULT 1 CHECK (next_sequence >= 1)
);
CREATE TABLE bravo_platform.state_owners (
    plan_id TEXT PRIMARY KEY REFERENCES bravo_platform.streams(plan_id)
);
CREATE TABLE bravo_platform.events (
    plan_id TEXT NOT NULL REFERENCES bravo_platform.streams(plan_id), sequence BIGINT NOT NULL,
    producer_key TEXT NOT NULL, request_hash TEXT NOT NULL, event_json TEXT NOT NULL,
    PRIMARY KEY (plan_id, sequence), UNIQUE (plan_id, producer_key)
);
CREATE TABLE bravo_platform.plans (
    plan_id TEXT PRIMARY KEY REFERENCES bravo_platform.streams(plan_id),
    spec_json TEXT NOT NULL, tenant_id TEXT NOT NULL,
    state TEXT NOT NULL, version BIGINT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE bravo_platform.plan_commands (
    plan_id TEXT NOT NULL REFERENCES bravo_platform.plans(plan_id),
    command_key TEXT NOT NULL, request_json TEXT NOT NULL, receipt_json TEXT NOT NULL,
    PRIMARY KEY (plan_id, command_key)
);
GRANT USAGE ON SCHEMA bravo_platform TO bravo_platform_runtime;
GRANT SELECT ON ALL TABLES IN SCHEMA bravo_platform TO bravo_platform_runtime;
GRANT INSERT ON bravo_platform.streams, bravo_platform.state_owners, bravo_platform.events,
    bravo_platform.plans, bravo_platform.plan_commands TO bravo_platform_runtime;
GRANT UPDATE (next_sequence) ON bravo_platform.streams TO bravo_platform_runtime;
GRANT UPDATE (state, version, updated_at) ON bravo_platform.plans TO bravo_platform_runtime;
INSERT INTO bravo_platform.schema_version VALUES (1);
COMMIT;
