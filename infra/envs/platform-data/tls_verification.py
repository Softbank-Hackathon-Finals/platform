"""Classify a plaintext attempt only between verified TLS connections to one endpoint."""
import hashlib
import json
import re
import time


def plaintext_rejection(error):
    # libpq connection errors often have no SQLSTATE/diag. Accept only PostgreSQL's
    # explicit English HBA rejection for this exact identity and unencrypted mode.
    # Other locales and all transport/authentication errors remain UNKNOWN.
    primary = getattr(getattr(error, 'diag', None), 'message_primary', None)
    messages = [primary] if primary else re.findall(r'FATAL:\s*([^\r\n]+)', str(error))
    pattern = (r'pg_hba\.conf rejects connection for host "[^"\r\n]+", '
               r'user "platform_runtime", database "platform", no encryption')
    return len(messages) == 1 and re.fullmatch(pattern, messages[0]) is not None


def verify_plaintext_rejection(conninfo, connect, connection_error):
    """The supplied DB driver keeps this policy independently testable without credentials."""
    observations, endpoint, generation = {}, None, None

    def record(name, operation):
        started = time.time_ns() // 1000000
        outcome = operation()
        finished = time.time_ns() // 1000000
        observations[name] = {'outcome': outcome, 'started_at_ms': started,
                              'finished_at_ms': finished, 'duration_ms': finished - started}
        return outcome

    def control():
        nonlocal endpoint, generation
        kwargs = {'sslmode': 'verify-full', 'gssencmode': 'disable', 'connect_timeout': 5,
                  'options': '-c statement_timeout=5000'}
        if endpoint:
            kwargs['hostaddr'] = endpoint[0]
            kwargs['port'] = endpoint[1]
        try:
            with connect(conninfo, **kwargs) as connection:
                row = connection.execute('SELECT ssl, version, current_user, current_database(), '
                    'pg_postmaster_start_time() FROM pg_stat_ssl WHERE pid=pg_backend_pid()').fetchone()
                actual = (connection.info.hostaddr, connection.info.port)
                if (not row or row[:4] not in ((True, 'TLSv1.2', 'platform_runtime', 'platform'),
                                              (True, 'TLSv1.3', 'platform_runtime', 'platform')) or not actual[0]):
                    return 'invalid_tls_identity'
                if endpoint is not None and (actual != endpoint or row[4] != generation):
                    return 'endpoint_changed'
                endpoint, generation = actual, row[4]
                return 'connected_tls'
        except connection_error:
            return 'unavailable'

    def attempt():
        try:
            with connect(conninfo, hostaddr=endpoint[0], port=endpoint[1], sslmode='disable',
                         gssencmode='disable', connect_timeout=5):
                return 'connected_plaintext'
        except connection_error as error:
            return 'rejected_no_encryption' if plaintext_rejection(error) else 'unclassified_error'

    started = time.monotonic()
    before = record('tls_before', control)
    if before == 'connected_tls':
        record('plaintext', attempt)
        record('tls_after', control)
    elapsed = int((time.monotonic() - started) * 1000)
    verdict = 'UNKNOWN'
    if (before == 'connected_tls' and observations['tls_after']['outcome'] == 'connected_tls'
            and elapsed <= 60000):
        outcome = observations['plaintext']['outcome']
        if outcome == 'rejected_no_encryption':
            verdict = 'PASS'
        elif outcome == 'connected_plaintext':
            verdict = 'FAIL'
    return {'verdict': verdict, 'observations': observations, 'duration_ms': elapsed, 'window_limit_ms': 60000,
            'endpoint_sha256': hashlib.sha256(json.dumps(endpoint).encode()).hexdigest() if endpoint else None}
