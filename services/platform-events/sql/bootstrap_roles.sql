-- Once per dedicated test/proposed platform database, as its administrator.
-- Login credentials and role membership are provisioned separately; no secrets here.
BEGIN;
CREATE ROLE bravo_platform_migrator NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE bravo_platform_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA bravo_platform AUTHORIZATION bravo_platform_migrator;
REVOKE ALL ON SCHEMA bravo_platform FROM PUBLIC;
COMMIT;
