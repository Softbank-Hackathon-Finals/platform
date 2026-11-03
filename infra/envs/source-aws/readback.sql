-- T6/T8/T34 read-only inspection in the application database.
-- Use the read-only inspection role and an existing TLS-protected connection.
-- No credentials, connection strings, business rows or audit message bodies.
BEGIN READ ONLY;
SELECT name, setting, pending_restart
FROM pg_settings
WHERE name IN ('server_version', 'wal_level', 'max_replication_slots',
               'max_wal_senders', 'shared_preload_libraries',
               'log_connections', 'pgaudit.log', 'pgaudit.log_parameter')
ORDER BY name;

SELECT extname, extversion FROM pg_extension WHERE extname = 'pgaudit';

SELECT rolname, rolsuper, rolreplication, rolcanlogin
FROM pg_roles WHERE rolname IN ('chat_app', 'replication', 'cutover_control')
ORDER BY rolname;

SELECT pubname, puballtables, pubinsert, pubupdate, pubdelete, pubtruncate
FROM pg_publication ORDER BY pubname;

SELECT pubname, schemaname, tablename
FROM pg_publication_tables ORDER BY pubname, schemaname, tablename;

-- Any returned table needs a PK decision before T8 replication.
SELECT n.nspname AS schema_name, c.relname AS table_name
FROM pg_class AS c
JOIN pg_namespace AS n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p') AND n.nspname = 'public'
  AND NOT EXISTS (SELECT 1 FROM pg_constraint AS p
                  WHERE p.conrelid = c.oid AND p.contype = 'p')
ORDER BY n.nspname, c.relname;
ROLLBACK;
