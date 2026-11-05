#!/usr/bin/env bash
# Local-only integration test. Creates and removes its own temporary PostgreSQL.
set -eu
umask 077
if [ "$#" -ne 1 ]; then
  printf '%s\n' 'usage: verify_real_demo_db.sh <modified-demo-chat-directory>' >&2
  exit 2
fi
demo_dir=$(cd "$1" && pwd)
runner_dir=$(cd "$(dirname "$0")/.." && pwd)
pgbin=$(pg_config --bindir)
[ -x "$demo_dir/.venv/bin/uvicorn" ] || { printf '%s\n' 'demo-chat virtual environment is missing' >&2; exit 2; }
pgdir=$(mktemp -d "${TMPDIR:-/tmp}/t15-real-app-XXXXXX")
app_pid=''
cleanup() {
  if [ -n "$app_pid" ]; then kill "$app_pid" 2>/dev/null || true; wait "$app_pid" 2>/dev/null || true; fi
  if [ -e "$pgdir/data/postmaster.pid" ]; then
    if ! "$pgbin/pg_ctl" -D "$pgdir/data" status >/dev/null 2>&1 ||
       ! "$pgbin/pg_ctl" -D "$pgdir/data" -m fast -w stop >/dev/null 2>&1; then
      printf '%s\n' 'temporary PostgreSQL cleanup could not be confirmed' >&2
      return 1
    fi
  fi
  python3 - "$pgdir" <<'PYCODE'
import shutil, sys
shutil.rmtree(sys.argv[1])
PYCODE
}
trap cleanup EXIT
read -r pgport appport < <(python3 - <<'PY'
import socket
sockets = [socket.socket(), socket.socket()]
for item in sockets:
    item.bind(("127.0.0.1", 0))
print(*(item.getsockname()[1] for item in sockets))
PY
)
role="t15_$(python3 -c 'import secrets; print(secrets.token_hex(8))')"
password=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
rotated=$(python3 -c 'import secrets; print(secrets.token_hex(16))')
"$pgbin/initdb" -D "$pgdir/data" --auth-local=trust --auth-host=scram-sha-256 --no-locale >/dev/null
"$pgbin/pg_ctl" -D "$pgdir/data" -o "-h 127.0.0.1 -p $pgport -k $pgdir" -l "$pgdir/server.log" -w start >/dev/null
"$pgbin/psql" -h "$pgdir" -p "$pgport" -d postgres -v ON_ERROR_STOP=1 >/dev/null <<SQL
CREATE ROLE $role LOGIN PASSWORD '$password';
CREATE DATABASE $role OWNER $role;
SQL
printf '{"username":"%s","password":"%s"}' "$role" "$password" > "$pgdir/credentials.json"
cd "$demo_dir"
APP_ENV=local DB_HOST=127.0.0.1 DB_PORT="$pgport" DB_NAME="$role" DB_CREDENTIAL_FILE="$pgdir/credentials.json" S3_BUCKET=fixture AWS_ACCESS_KEY_ID=fixture AWS_SECRET_ACCESS_KEY=fixture AWS_DEFAULT_REGION=ap-northeast-2 AWS_EC2_METADATA_DISABLED=true .venv/bin/uvicorn app.main:create_app --factory --host 127.0.0.1 --port "$appport" >"$pgdir/app.log" 2>&1 &
app_pid=$!
probe() {
  PYTHONPATH="$runner_dir/src" T15_APP_PORT="$appport" T15_EXPECT="$1" python3 - <<'PY'
import os
import socket
import time
from rehearsal_runner.app_health_probe import observe_app_database
original = socket.getaddrinfo
def local_dns(host, *args, **kwargs):
    if host == "candidate.rehearsal-fixture.svc.cluster.local":
        host = "127.0.0.1"
    return original(host, *args, **kwargs)
socket.getaddrinfo = local_dns
expected = os.environ["T15_EXPECT"]
for _ in range(30):
    result = observe_app_database("rehearsal-fixture", "candidate", int(os.environ["T15_APP_PORT"]), timeout=4)
    if result == expected:
        break
    time.sleep(.5)
else:
    raise SystemExit("DB observation did not reach expected state")
PY
}
probe yes
printf '%s\n' 'initial authenticated DB connection: yes'
"$pgbin/psql" -h "$pgdir" -p "$pgport" -d postgres -v ON_ERROR_STOP=1 >/dev/null <<SQL
ALTER ROLE $role PASSWORD '$rotated';
SQL
printf '{"username":"%s","password":"%s"}' "$role" "$rotated" > "$pgdir/credentials.json"
"$pgbin/psql" -h "$pgdir" -p "$pgport" -d postgres -v ON_ERROR_STOP=1 >/dev/null <<SQL
SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='$role' AND datname='$role';
SQL
probe yes
printf '%s\n' 'rotated credentials and new DB connection: yes'
"$pgbin/pg_ctl" -D "$pgdir/data" -m immediate -w stop >/dev/null
probe no
printf '%s\n' 'DB unavailable observation: no'
