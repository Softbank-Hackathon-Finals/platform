"""Actual pre-cutover pooler path, TLS and rolled-back synthetic application write."""
import json
import os
import shlex
import time
import uuid
from pathlib import Path

import psycopg

STAGE = "configuration"
CHECKS = {}


def main():
    global STAGE
    credentials = dict(shlex.split(line) for line in Path('/auth/userlist.txt').read_text().splitlines())
    args = {'host': 'target-pgbouncer.target-dev.svc.cluster.local', 'dbname': 'chat',
            'user': 'chat_app', 'password': os.environ['APP_PASSWORD'],
            'sslmode': 'verify-full', 'sslrootcert': '/tls/ca.crt', 'connect_timeout': 10}
    checks = CHECKS
    started = time.monotonic()
    STAGE = "initial_connection"
    with psycopg.connect(**args) as conn:
        connect_seconds = time.monotonic() - started
        # libpq verify-full succeeds only with TLS and a matching trusted certificate.
        checks['client_verify_full_connection'] = not conn.closed
        STAGE = "source_backend_query"
        with conn.cursor() as cur:
            cur.execute("SELECT current_user,current_setting('cluster_name'),ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()")
            role, cluster, ssl = cur.fetchone()
            checks['source_backend_app_role_and_tls'] = role == 'chat_app' and cluster == 'source-db' and ssl
            cur.execute('SELECT (SELECT count(*) FROM messages),(SELECT count(*) FROM attachments)')
            checks['original_rows_readable'] = cur.fetchone() == (2, 1)
        conn.rollback()
        STAGE = "rollback_write"
        key = str(uuid.uuid4())
        started = time.monotonic()
        with conn.cursor() as cur:
            cur.execute("INSERT INTO messages(idempotency_key,author,body) VALUES(%s,'T8 pooler rollback','synthetic transaction, always rollback') RETURNING id", (key,))
            checks['source_application_insert_authorized'] = cur.fetchone()[0] > 0
        conn.rollback()
        rollback_write_seconds = time.monotonic() - started
        with conn.cursor() as cur:
            cur.execute('SELECT count(*) FROM messages WHERE idempotency_key=%s', (key,))
            checks['synthetic_write_rolled_back'] = cur.fetchone()[0] == 0
    STAGE = 'pooler_admin'
    with psycopg.connect(**{**args, 'dbname': 'pgbouncer', 'user': 'cutover_control',
                           'password': credentials['cutover_control']}, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute('SHOW CONFIG'); config = {row[0]: row[1] for row in cur.fetchall()}
        checks['verified_server_tls_config'] = config['server_tls_sslmode'] == 'verify-full'
        checks['reviewed_admin_retry_config'] = config['admin_users'] == 'cutover_control' and config['server_login_retry'] == '2'
    STAGE = 'negative_connections'
    for name, changes in [('wrong_password_denied', {'password': 'intentionally-wrong'}),
                           ('wrong_tls_hostname_denied', {'host': 'wrong-host.invalid', 'hostaddr': os.environ['POOLER_IP']})]:
        try:
            with psycopg.connect(**{**args, **changes}):
                checks[name] = False
        except psycopg.OperationalError as error:
            checks[name] = (error.sqlstate in {'28P01', '28000'} if name == 'wrong_password_denied'
                            else 'certificate' in str(error).lower() and 'match' in str(error).lower())
    with psycopg.connect(**args) as conn:
        checks['positive_connection_after_negative_tests'] = conn.execute('SELECT 1').fetchone() == (1,)
    print(json.dumps({'passed': all(checks.values()), 'checks': checks,
                      'connect_seconds': connect_seconds, 'rollback_write_seconds': rollback_write_seconds,
                      'timing_scope': 'single emulated-development probe, not p95 or app SLO',
                      'business_write_flags_changed': False, 'app_startup_verified': False}))
    return 0 if all(checks.values()) else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, AttributeError, psycopg.Error) as error:
        print(json.dumps({'passed': False, 'stage': STAGE, 'checks': CHECKS, 'error_category': type(error).__name__,
                          'sqlstate': error.sqlstate if isinstance(error, psycopg.Error) else None,
                          'timeout_marker': 'timeout' in str(error).lower(),
                          'authentication_marker': 'authentication' in str(error).lower(),
                          'certificate_marker': 'certificate' in str(error).lower(),
                          'raw_output_suppressed': True}))
        raise SystemExit(1) from None
