"""Reviewed, fixed-target PostgreSQL control surface. Emits no credentials.

An operator provisions the LOGIN role/password separately. Existing table and
subscription ownership is preserved; the runtime never receives their owners.
"""

import re


def control_sql(subscription: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", subscription):
        raise ValueError("invalid subscription identifier")
    return f"""BEGIN;
DO $check$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='cutover_control') THEN
    RAISE EXCEPTION 'provision the runtime role first';
  END IF;
  IF EXISTS (SELECT FROM pg_roles WHERE rolname='cutover_control'
             AND (rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls))
     OR EXISTS (SELECT FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member
                JOIN pg_roles granted ON granted.oid=m.roleid
                WHERE r.rolname='cutover_control' AND granted.rolname <> 'pg_read_all_stats') THEN
    RAISE EXCEPTION 'runtime role has unexpected elevated privileges';
  END IF;
  IF NOT EXISTS (SELECT FROM pg_subscription WHERE subname='{subscription}'
                 AND subdbid=(SELECT oid FROM pg_database WHERE datname=current_database())) THEN
    RAISE EXCEPTION 'expected subscription is missing';
  END IF;
  IF EXISTS (SELECT FROM pg_namespace WHERE nspname='cutover' AND nspowner <> 'postgres'::regrole) THEN
    RAISE EXCEPTION 'unexpected control schema owner';
  END IF;
END $check$;
CREATE SCHEMA IF NOT EXISTS cutover AUTHORIZATION postgres;
REVOKE CREATE ON SCHEMA cutover FROM PUBLIC, cutover_control, chat_app;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA cutover REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
CREATE TABLE IF NOT EXISTS cutover.barrier (
    barrier_id uuid PRIMARY KEY, epoch bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS cutover.probe (
    probe_id uuid PRIMARY KEY, path text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
GRANT USAGE ON SCHEMA cutover TO cutover_control, chat_app, chat_replicator;
GRANT SELECT, INSERT ON cutover.barrier TO cutover_control;
GRANT SELECT ON cutover.probe, public.messages, public.attachments TO cutover_control;
GRANT SELECT ON SEQUENCE public.messages_id_seq TO cutover_control;
GRANT INSERT ON cutover.probe TO chat_app;
GRANT SELECT ON cutover.barrier, cutover.probe TO chat_replicator;
GRANT pg_read_all_stats TO cutover_control;

CREATE OR REPLACE FUNCTION cutover.set_app_login(allowed boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $function$
BEGIN
  IF allowed IS NULL THEN RAISE EXCEPTION 'allowed is required' USING ERRCODE='22004'; END IF;
  IF allowed THEN ALTER ROLE chat_app LOGIN; ELSE ALTER ROLE chat_app NOLOGIN; END IF;
END $function$;

CREATE OR REPLACE FUNCTION cutover.terminate_app_sessions() RETURNS bigint
LANGUAGE sql SECURITY DEFINER SET search_path=pg_catalog AS $function$
  SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity
  WHERE usename='chat_app' AND pid<>pg_backend_pid()
$function$;

CREATE OR REPLACE FUNCTION cutover.set_subscription(requested text, enabled boolean) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $function$
BEGIN
  IF requested IS DISTINCT FROM '{subscription}' OR enabled IS NULL THEN
    RAISE EXCEPTION 'subscription is not authorized' USING ERRCODE='42501';
  END IF;
  IF enabled THEN ALTER SUBSCRIPTION "{subscription}" ENABLE;
  ELSE ALTER SUBSCRIPTION "{subscription}" DISABLE; END IF;
END $function$;

CREATE OR REPLACE FUNCTION cutover.raise_messages_sequence(wanted bigint) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $function$
DECLARE current_next bigint; called boolean; step bigint; maximum bigint;
BEGIN
  IF wanted IS NULL OR wanted < 1 THEN RAISE EXCEPTION 'invalid next value' USING ERRCODE='22023'; END IF;
  PERFORM pg_advisory_xact_lock(7131112);
  SELECT last_value,is_called INTO current_next,called FROM public.messages_id_seq;
  SELECT seqincrement INTO step FROM pg_sequence WHERE seqrelid='public.messages_id_seq'::regclass;
  IF step <> 1 THEN RAISE EXCEPTION 'only ascending unit sequence supported'; END IF;
  IF called THEN current_next := current_next+step; END IF;
  SELECT coalesce(max(id),0)+1 INTO maximum FROM public.messages;
  wanted := greatest(wanted,maximum,current_next);
  IF wanted > current_next THEN PERFORM setval('public.messages_id_seq'::regclass,wanted,false); END IF;
  RETURN wanted;
END $function$;

ALTER FUNCTION cutover.set_app_login(boolean) OWNER TO postgres;
ALTER FUNCTION cutover.terminate_app_sessions() OWNER TO postgres;
ALTER FUNCTION cutover.set_subscription(text,boolean) OWNER TO postgres;
ALTER FUNCTION cutover.raise_messages_sequence(bigint) OWNER TO postgres;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA cutover FROM PUBLIC, chat_app, chat_replicator;
GRANT EXECUTE ON FUNCTION cutover.set_app_login(boolean), cutover.terminate_app_sessions(),
    cutover.set_subscription(text,boolean), cutover.raise_messages_sequence(bigint) TO cutover_control;
COMMIT;
"""


def state_runtime_grants_sql(role: str = "cutover_executor_runtime") -> str:
    """Apply after migration; no ownership or DDL grant reaches the API runtime.

    `role` is the runtime login of one Executor instance (deploy.py `--pair`: its own state database and role).
    """
    if not re.fullmatch(r"cutover_executor(_[a-z][a-z0-9]{0,15})?_runtime", role):
        raise ValueError("invalid runtime role")
    return f"""REVOKE ALL ON SCHEMA executor FROM PUBLIC, {role};
GRANT USAGE ON SCHEMA executor TO {role};
REVOKE ALL ON ALL TABLES IN SCHEMA executor FROM {role};
GRANT SELECT, INSERT, UPDATE ON executor.executions, executor.resource_fences, executor.leases
    TO {role};
"""
