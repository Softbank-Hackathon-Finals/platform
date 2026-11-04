#!/usr/bin/env python3
"""T12 모의 환경: k3d 클러스터 하나에 출발지(src) · 목적지(dst)를 네임스페이스로 띄운다 (spec D9).

    python3 testing/mockenv.py up      # 생성 (이미 있으면 클러스터는 그대로 두고 다시 적용하지 않는다)
    python3 testing/mockenv.py check   # 복제 · 쓰기 경로 · 관리 콘솔 확인. 실패하면 종료 코드 1
    python3 testing/mockenv.py env     # 접속 정보(JSON). 비밀번호 포함, 화면 공유 주의
    python3 testing/mockenv.py sites   # Executor 설정 testing/.state/sites.json 다시 쓰기
    python3 testing/mockenv.py down    # 삭제

각 네임스페이스: Postgres 16(wal_level=logical) · PgBouncer(설정은 쓰기 가능한 emptyDir) · demo-chat 2개.
aws 네임스페이스: moto(Secrets Manager · S3). 복제는 T8 전까지 직접 만든 구독으로 흉내 낸다:
dst의 bravo_fwd(src → dst, 켜짐), src의 bravo_rev(dst → src, 꺼짐 · copy_data=false).
비밀번호는 실행마다 새로 만들고 testing/.state/env.json(git 제외)에만 둔다.
"""

import json
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from string import Template

HERE = Path(__file__).resolve().parent
STATE = HERE / ".state" / "env.json"
SITES_FILE = HERE / ".state" / "sites.json"  # Executor 설정(bravo_cutover.sites)
CLUSTER = "bravo-cutover"
CONTEXT = f"k3d-{CLUSTER}"
K3S_IMAGE = "rancher/k3s:v1.35.5-k3s1"
POSTGRES_IMAGE = "postgres@sha256:5db6dac2ef5dcff512f4f00a009c3c332b9f2f28f24277175bf3e4a3d02ef83a"  # 16
PGBOUNCER_IMAGE = "edoburu/pgbouncer@sha256:8638aa563607256d3c7440437534195f1066a92b68015d89ae9d6b935b444a6d"  # 1.26.1
DEMO_CHAT_DIR = HERE.parents[3] / "demo-chat"
DEMO_CHAT_REF = "origin/main"

SITES = {
    "src": {"app_env": "aws", "environment_id": "env-aws", "pg": 30432, "pgb": 30433, "app": 30480},
    "dst": {"app_env": "onprem", "environment_id": "env-onprem", "pg": 30532, "pgb": 30533, "app": 30580},
}
UPSTREAM = "postgres.src.svc.cluster.local"  # 두 PgBouncer 모두 처음엔 출발지 DB


def run(*args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=input, text=True, capture_output=True, check=check)


def kubectl(*args: str, input: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return run("kubectl", "--context", CONTEXT, *args, input=input, check=check)


def psql(ns: str, sql: str, *, variables: dict[str, str] | None = None, conninfo: str | None = None) -> str:
    """파드 안에서 psql을 돌린다. conninfo가 없으면 로컬 소켓의 postgres(슈퍼유저)로 붙는다."""
    cmd = ["exec", "-i", "-n", ns, "deploy/postgres", "--", "psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1"]
    for k, v in (variables or {}).items():
        cmd += ["-v", f"{k}={v}"]
    cmd += [conninfo] if conninfo else ["-U", "postgres", "-d", "chat"]
    return kubectl(*cmd, input=sql).stdout.strip()


def load_state() -> dict:
    return json.loads(STATE.read_text())


def build_app_image() -> str:
    sha = run("git", "-C", str(DEMO_CHAT_DIR), "rev-parse", "--short=12", DEMO_CHAT_REF).stdout.strip()
    image = f"demo-chat:mock-{sha}"
    with tempfile.TemporaryDirectory() as tmp:
        archive = subprocess.run(
            ["git", "-C", str(DEMO_CHAT_DIR), "archive", DEMO_CHAT_REF], capture_output=True, check=True
        ).stdout
        subprocess.run(["tar", "-x", "-C", tmp], input=archive, check=True)
        run("docker", "build", "-q", "-t", image, tmp)
    run("k3d", "image", "import", "-c", CLUSTER, image)
    return image


def app_schema() -> str:
    return run("git", "-C", str(DEMO_CHAT_DIR), "show", f"{DEMO_CHAT_REF}:db/schema.sql").stdout


def create_cluster() -> None:
    if run("k3d", "cluster", "get", CLUSTER, check=False).returncode == 0:
        return
    ports = [f"{p}:{p}@server:0" for site in SITES.values() for p in (site["pg"], site["pgb"], site["app"])]
    run(
        "k3d", "cluster", "create", CLUSTER, "--image", K3S_IMAGE, "--servers", "1",
        "--k3s-arg", "--disable=traefik@server:0",
        *[a for p in ports for a in ("--port", p)],
        "--kubeconfig-update-default", "--kubeconfig-switch-context=false", "--wait",
    )


def credentials_secret(ns: str, creds: dict[str, str]) -> str:
    return json.dumps({
        "apiVersion": "v1", "kind": "Secret", "metadata": {"name": "mock-credentials", "namespace": ns},
        "stringData": creds,
    })


def up() -> None:
    for cmd in ("docker", "k3d", "kubectl", "git"):
        if run("which", cmd, check=False).returncode:
            sys.exit(f"{cmd} 이 없다.")
    if STATE.exists() and run("k3d", "cluster", "get", CLUSTER, check=False).returncode == 0:
        print(f"이미 있다: {CLUSTER}. 다시 만들려면 down 뒤 up.")
        return
    create_cluster()
    creds = {
        "postgres-password": secrets.token_urlsafe(18),
        "chat-app-password": secrets.token_urlsafe(18),
        "control-password": secrets.token_urlsafe(18),
        "replicator-password": secrets.token_urlsafe(18),
        "admin-token": secrets.token_urlsafe(24),
    }
    image = build_app_image()

    kubectl("apply", "-f", "-", input=(HERE / "manifests" / "aws.yaml").read_text().split("---", 1)[0])
    kubectl("apply", "-f", "-", input=credentials_secret("aws", creds))
    kubectl("apply", "-f", str(HERE / "manifests" / "aws.yaml"))
    users = "".join(f'"{u}" "{creds[k]}"\n' for u, k in (("chat_app", "chat-app-password"), ("cutover_control", "control-password")))
    template = Template((HERE / "manifests" / "site.yaml").read_text())
    for ns, site in SITES.items():
        manifest = template.substitute(
            NS=ns, APP_ENV=site["app_env"], PG_NODEPORT=site["pg"], PGB_NODEPORT=site["pgb"],
            APP_NODEPORT=site["app"], UPSTREAM_HOST=UPSTREAM, POSTGRES_IMAGE=POSTGRES_IMAGE,
            PGBOUNCER_IMAGE=PGBOUNCER_IMAGE, APP_IMAGE=image,
        )
        docs = manifest.split("\n---\n")
        kubectl("apply", "-f", "-", input=docs[0])  # 네임스페이스 먼저
        kubectl("apply", "-f", "-", input=credentials_secret(ns, creds))
        kubectl("apply", "-f", "-", input=kubectl(
            "create", "secret", "generic", "pgbouncer-users", "-n", ns,
            "--from-literal=userlist.txt=" + users, "--dry-run=client", "-o", "yaml").stdout)
        kubectl("apply", "-f", "-", input="\n---\n".join(docs[1:]))

    for ns in SITES:
        kubectl("rollout", "status", "-n", ns, "deploy/postgres", "--timeout=180s")
    roles = (HERE / "sql" / "10-roles.sql").read_text()
    cutover = (HERE / "sql" / "30-cutover.sql").read_text()
    schema = app_schema()
    for ns in SITES:
        psql(ns, roles, variables={
            "chat_app_pw": creds["chat-app-password"], "replicator_pw": creds["replicator-password"],
            "control_pw": creds["control-password"],
            "chat_app_login": "LOGIN" if ns == "src" else "NOLOGIN",
        })
        psql(ns, schema)
        psql(ns, cutover)

    def sub_sql(name: str, publisher: str, options: str) -> str:
        conn = (f"host=postgres.{publisher}.svc.cluster.local port=5432 dbname=chat "
                f"user=replicator password={creds['replicator-password']}")
        return (f"SET ROLE subscriber;\nCREATE SUBSCRIPTION {name} CONNECTION '{conn}' "
                f"PUBLICATION bravo_pub WITH ({options});\n")

    psql("dst", sub_sql("bravo_fwd", "src", "origin = none, run_as_owner = true"))
    psql("src", sub_sql("bravo_rev", "dst", "enabled = false, copy_data = false, origin = none, run_as_owner = true"))

    kubectl("wait", "-n", "aws", "--for=condition=complete", "job/seed", "--timeout=180s")
    for ns in SITES:
        for deploy in ("pgbouncer", "demo-chat"):
            kubectl("rollout", "status", "-n", ns, f"deploy/{deploy}", "--timeout=180s")

    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({
        "context": CONTEXT,
        "demo_chat_image": image,
        "credentials": creds,
        "sites": {
            ns: {
                "namespace": ns,
                "db": {"host": "localhost", "port": s["pg"], "dbname": "chat"},
                "pgbouncer": {"host": "localhost", "port": s["pgb"]},
                "app_url": f"http://localhost:{s['app']}",
            }
            for ns, s in SITES.items()
        },
    }, indent=2))
    STATE.chmod(0o600)
    write_sites()
    print(f"준비됨: {CONTEXT}, demo-chat {image}. 확인: python3 testing/mockenv.py check")


def write_sites() -> None:
    """Executor가 읽는 환경 설정. 키는 environment_id다. 호스트에서 NodePort로 붙는다."""
    creds = load_state()["credentials"]

    def dsn(port: int, dbname: str, user: str, key: str) -> str:
        return f"host=localhost port={port} dbname={dbname} user={user} password={creds[key]}"

    sites = {
        s["environment_id"]: {
            "environment_id": s["environment_id"],
            # 실제 DB 자원 ID와 허용 접속 참조(lease · epoch는 자원마다). 시험 요청의 connection_ref와 같다.
            "resource_id": f"mock-{ns}/chat",
            "connection_ref": f"mock/{ns}",
            "kube_context": CONTEXT,
            "namespace": ns,
            "pgbouncer_admin_dsn": dsn(s["pgb"], "pgbouncer", "cutover_control", "control-password"),
            "pgbouncer_app_dsn": dsn(s["pgb"], "chat", "chat_app", "chat-app-password"),
            "db_control_dsn": dsn(s["pg"], "chat", "cutover_control", "control-password"),
            "db_app_dsn": dsn(s["pg"], "chat", "chat_app", "chat-app-password"),
            "db_host": f"postgres.{ns}.svc.cluster.local",
            "subscription": "bravo_rev" if ns == "src" else "bravo_fwd",
        }
        for ns, s in SITES.items()
    }
    config = {"settings": {}, "sites": sites}
    SITES_FILE.write_text(json.dumps(config, indent=2))
    SITES_FILE.chmod(0o600)


def post_message(url: str, key: uuid.UUID, body: str, author: str = "mockenv", retry_s: float = 20) -> int:
    """앱에 메시지를 쓴다. 503이면 Retry-After만큼 기다렸다가 같은 멱등 키로 다시 보낸다(demo-chat 클라이언트 규칙).
    쓰기 정지 · DB 경로 변경 직후 앱 커넥션 풀이 끊긴 연결을 버리는 동안 잠시 503이 난다."""
    deadline = time.monotonic() + retry_s
    while True:
        req = urllib.request.Request(
            f"{url}/api/messages", method="POST",
            data=json.dumps({"idempotency_key": str(key), "author": author, "body": body}).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as res:
                return res.status
        except urllib.error.HTTPError as e:
            if e.code != 503 or time.monotonic() > deadline:
                return e.code
            time.sleep(float(e.headers.get("Retry-After") or 1))


def wait_row(ns: str, key: uuid.UUID, timeout: float = 15) -> float | None:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if psql(ns, f"SELECT count(*) FROM messages WHERE idempotency_key = '{key}'") == "1":
            return round(time.monotonic() - start, 2)
        time.sleep(0.5)
    return None


def check() -> None:
    state = load_state()
    creds = state["credentials"]
    results = {}

    for ns in SITES:
        conn = (f"host=pgbouncer.{ns}.svc.cluster.local port=6432 dbname=pgbouncer "
                f"user=cutover_control password={creds['control-password']}")
        try:
            results[f"{ns}_pgbouncer_admin"] = psql(ns, "SHOW VERSION;", conninfo=conn)
        except subprocess.CalledProcessError as e:
            results[f"{ns}_pgbouncer_admin"] = f"FAIL: {e.stderr.strip()}"

    key = uuid.uuid4()
    results["src_app_write"] = post_message(state["sites"]["src"]["app_url"], key, "src → dst 복제 확인")
    lag = wait_row("dst", key)
    results["src_write_replicated_to_dst_s"] = lag

    key = uuid.uuid4()
    results["dst_app_write"] = post_message(state["sites"]["dst"]["app_url"], key, "목적지 앱 → 출발지 DB")
    results["dst_app_write_lands_in_src"] = psql("src", f"SELECT count(*) FROM messages WHERE idempotency_key = '{key}'") == "1"

    # 목적지 앱 역할이 닫혀 있는지: 카탈로그 값과, 맞는 비밀번호로 접속했을 때 "로그인 금지"로 거부되는지를 함께 본다.
    # 비밀번호 오류 · 통신 실패 같은 다른 실패를 거부로 세지 않는다.
    results["dst_chat_app_rolcanlogin"] = psql("dst", "SELECT rolcanlogin FROM pg_roles WHERE rolname = 'chat_app'")
    conn = f"host=postgres.dst.svc.cluster.local port=5432 dbname=chat user=chat_app password={creds['chat-app-password']}"
    try:
        psql("dst", "SELECT 1;", conninfo=conn)
        results["dst_chat_app_login_refused"] = False
    except subprocess.CalledProcessError as e:
        results["dst_chat_app_login_refused"] = "is not permitted to log in" in e.stderr
        if not results["dst_chat_app_login_refused"]:
            results["dst_chat_app_login_error"] = e.stderr.strip()[-200:]
    results["subscriptions"] = {
        ns: psql(ns, "SELECT string_agg(subname || '=' || subenabled, ',') FROM pg_subscription") for ns in SITES
    }

    print(json.dumps(results, ensure_ascii=False, indent=2))
    ok = (
        all(str(results[f"{ns}_pgbouncer_admin"]).startswith("PgBouncer") for ns in SITES)
        and results["src_app_write"] in (200, 201)
        and lag is not None
        and results["dst_app_write"] in (200, 201)
        and results["dst_app_write_lands_in_src"]
        and results["dst_chat_app_login_refused"]
        and results["dst_chat_app_rolcanlogin"] == "f"
        and results["subscriptions"] == {"src": "bravo_rev=false", "dst": "bravo_fwd=true"}
    )
    sys.exit(0 if ok else 1)


def down() -> None:
    run("k3d", "cluster", "delete", CLUSTER, check=False)
    STATE.unlink(missing_ok=True)
    SITES_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    commands = {"up": up, "check": check, "down": down, "sites": write_sites,
                "env": lambda: print(STATE.read_text())}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit(f"사용법: {sys.argv[0]} [{'|'.join(commands)}]")
    try:
        commands[sys.argv[1]]()
    except subprocess.CalledProcessError as e:
        sys.exit(f"실패: {' '.join(e.cmd[:6])} …\n{e.stderr}")
