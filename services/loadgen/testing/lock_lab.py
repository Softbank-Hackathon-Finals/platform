#!/usr/bin/env python3
"""demo-chat 쓰기 잠금 실험. 왕복 수 · 처리량 천장 · 불변식 · 지연 행렬을 개선안마다 잰다. 로컬 시험 전용이다.

    uv run python testing/lock_lab.py <명령> --variants <개선안 폴더들의 부모> --out <산출물 폴더>

- 개선안 폴더는 demo-chat을 고친 사본이다. 폴더 이름이 개선안 이름이다.
- 컨테이너는 lock-lab-로 시작하고 끝나면 지운다. 비밀번호는 실행마다 새로 만들고 파일에 남기지 않는다.
- 지연은 tcp_fault.py로 넣은 모의값이다. 실제 클라우드 사이 값이 아니다.
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import threading
import time
from collections import Counter
from pathlib import Path

import psycopg
from e2e import Harness, Stack, free_port, judged, run_quiet

HERE = Path(__file__).resolve().parent
PREFIX = "lock-lab"
PG16 = "public.ecr.aws/docker/library/postgres:16"
PG18 = "public.ecr.aws/docker/library/postgres:18.4"
# infra/envs/source-workloads/render.py의 POOLER_IMAGE와 같다.
PGBOUNCER_IMAGE = (
    "ghcr.io/cloudnative-pg/pgbouncer:1.25.1"
    "@sha256:e6ddfe22d845e603825e235dd8334b21ecd125abea2a2172478f556b8dee2bb8"
)
PGBOUNCER_PORT = 6432
# infra/envs/source-workloads/pgbouncer.ini에서 TLS를 빼고, 끊김 시험이 오래 멈추지 않게 로그인 재시도만 줄였다.
PGBOUNCER_INI = """
[databases]
chat = host={host} port={db_port} dbname=chat

[pgbouncer]
listen_addr = 0.0.0.0
listen_port = {port}
unix_socket_dir = /tmp
auth_type = scram-sha-256
auth_file = /tmp/lab/userlist.txt
admin_users = postgres
pool_mode = transaction
max_client_conn = 100
default_pool_size = 10
max_prepared_statements = 0
ignore_startup_parameters = extra_float_digits,options
server_login_retry = 1
"""


class BouncerStack(Stack):
    """설계 8.6의 전환 구간 구성이다. 앱마다 자기 쪽 PgBouncer(transaction 모드)를 거쳐 출발지 DB에 붙는다.

    출발지 앱 → 출발지 PgBouncer → DB, 목적지 앱 → 목적지 PgBouncer → (왕복 지연) → DB.
    대조와 잠금 측정은 DB에 직접 붙는다.
    """

    def _start_postgres(self) -> None:
        super()._start_postgres()
        self.network = f"{PREFIX}-net-{self.tag}"
        run_quiet("docker", "network", "create", self.network)
        run_quiet("docker", "network", "connect", self.network, self.pg_name)
        self.bouncers: dict[str, int] = {}
        self.app_db_port = self.start_bouncer("source", self.pg_name, 5432)

    def start_bouncer(self, name: str, host: str, port: int) -> int:
        """host:port를 가리키는 PgBouncer를 띄우고 앱이 붙을 포트를 돌려준다."""
        published = free_port()
        container = f"{PREFIX}-bouncer-{name}-{self.tag}"
        run_quiet(
            "docker", "create", "--name", container, "--network", self.network,
            "--add-host=host.docker.internal:host-gateway",
            "-p", f"127.0.0.1:{published}:{PGBOUNCER_PORT}", "--entrypoint", "pgbouncer",
            PGBOUNCER_IMAGE, "/tmp/lab/pgbouncer.ini",
        )  # fmt: skip
        self.containers.append(container)
        files = {
            "lab/pgbouncer.ini": PGBOUNCER_INI.format(host=host, db_port=port, port=PGBOUNCER_PORT),
            "lab/userlist.txt": f'"chat_app" "{self.app_password}"\n"postgres" "{self.admin_password}"\n',
        }
        # 비밀번호가 디스크에 남지 않도록 파일 없이 tar를 바로 넘긴다.
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w") as tar:
            directory = tarfile.TarInfo("lab")
            directory.type, directory.mode = tarfile.DIRTYPE, 0o777
            tar.addfile(directory)
            for path, text in files.items():
                info = tarfile.TarInfo(path)
                data = text.encode()
                info.size, info.mode = len(data), 0o644
                tar.addfile(info, io.BytesIO(data))
        subprocess.run(["docker", "cp", "-", f"{container}:/tmp"], input=archive.getvalue(), check=True, capture_output=True)
        run_quiet("docker", "start", container)
        deadline = time.monotonic() + 30
        while True:
            try:
                with psycopg.connect(dsn_for(self.app_password, published), connect_timeout=2, prepare_threshold=None) as connection:
                    connection.execute("SELECT 1")
                break
            except psycopg.Error:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.25)
        self.bouncers[name] = published
        return published

    def delayed_db_port(self, rtt_ms: float) -> int:
        proxy_port = super().delayed_db_port(rtt_ms)
        return self.start_bouncer(f"target-{rtt_ms:g}", "host.docker.internal", proxy_port)

    def __exit__(self, *exc_info):
        super().__exit__(*exc_info)
        if getattr(self, "network", None):
            subprocess.run(["docker", "network", "rm", self.network], capture_output=True)

    def pause(self, rtt_ms: float) -> dict[str, float]:
        """두 PgBouncer에 PAUSE를 동시에 보내 돌아올 때까지 재고 바로 RESUME한다. 진행 중 트랜잭션이 끝나야 돌아온다."""
        result: dict[str, float] = {}

        def pause_one(role: str, port: int) -> None:
            dsn = f"postgresql://postgres:{self.admin_password}@127.0.0.1:{port}/pgbouncer"
            with psycopg.connect(dsn, autocommit=True, prepare_threshold=None) as console:
                started = time.monotonic()
                console.execute("PAUSE chat")
                result[role] = round((time.monotonic() - started) * 1000, 1)
                console.execute("RESUME chat")

        threads = [
            threading.Thread(target=pause_one, args=("source", self.bouncers["source"])),
            threading.Thread(target=pause_one, args=("target", self.bouncers[f"target-{rtt_ms:g}"])),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        return result


def db_stack(variant: Path, out: Path, *, bouncer: bool = False, image: str = PG16) -> Stack:
    """앱 없이 DB(와 풀러)만 띄운다. Store를 직접 부르는 시험용이다."""
    out.mkdir(parents=True, exist_ok=True)
    stack = (BouncerStack if bouncer else Stack)(variant, out, container_prefix=PREFIX, postgres_image=image)
    stack._require_same_lock_key()
    try:
        stack._start_postgres()
    except BaseException:
        stack.__exit__()
        raise
    return stack


def driver(variant: Path, command: str, config: dict, timeout: float = 900) -> dict:
    """개선안의 인터프리터로 lock_lab_driver.py를 돌린다. 설정과 비밀번호는 표준 입력으로만 넘긴다."""
    env = {**os.environ, "PYTHONPATH": str(variant)}
    result = subprocess.run(
        [str(variant / ".venv/bin/python"), str(HERE / "lock_lab_driver.py"), command],
        input=json.dumps(config), capture_output=True, text=True, env=env, cwd=variant, timeout=timeout,
    )  # fmt: skip
    if result.returncode != 0:
        # 예외 메시지에 연결 문자열이 섞일 수 있어 마지막 줄의 예외 이름만 남긴다.
        name = result.stderr.strip().splitlines()[-1].split(":")[0] if result.stderr.strip() else "unknown"
        return {"driver_error": name}
    return json.loads(result.stdout)


class Proxies:
    """Stack이 프로세스를 정리하도록 맡기는 중계기 묶음."""

    def __init__(self, stack: Stack):
        self.stack = stack

    def tap(self, target_port: int, log: Path, rtt_ms: float) -> int:
        port = free_port()
        self.stack.processes.append(
            subprocess.Popen(
                [sys.executable, str(HERE / "lock_lab_tap.py"), str(port), str(target_port), str(log), "--rtt-ms", str(rtt_ms)]
            )  # fmt: skip
        )
        time.sleep(0.5)
        return port


def dsn_for(password: str, port: int, *, user: str = "chat_app", extra: str = "") -> str:
    return f"postgresql://{user}:{password}@127.0.0.1:{port}/chat{extra}"


# ---------- 실제 왕복 ----------


class RttProbe:
    """중계기를 거친 SELECT 1을 재서 실제 왕복을 본다. 지연 설정값과 실제 값은 다를 수 있다."""

    def __init__(self, dsn: str, interval_s: float = 0.2):
        self.dsn = dsn
        self.interval_s = interval_s
        self.samples: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        try:
            with psycopg.connect(self.dsn, autocommit=True, prepare_threshold=None, connect_timeout=5) as connection:
                while not self._stop.is_set():
                    started = time.perf_counter()
                    connection.execute("SELECT 1")
                    self.samples.append((time.perf_counter() - started) * 1000)
                    self._stop.wait(self.interval_s)
        except psycopg.Error:
            return

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc_info):
        self._stop.set()
        self._thread.join(5)

    def summary(self) -> dict | None:
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        return {
            "samples": len(ordered),
            "p50_ms": round(ordered[len(ordered) // 2], 3),
            "p95_ms": round(ordered[int(len(ordered) * 0.95)], 3),
            "max_ms": round(ordered[-1], 3),
        }


def command_calibrate(args) -> None:
    """부하 없이 지연 설정값마다 실제 왕복을 잰다. DB에 직접 붙은 값을 함께 둔다."""
    variant = args.variants / args.names[0]
    stack = db_stack(variant, args.out / "stack")
    rows = []
    try:
        targets = [("direct", None, stack.pg_port)] + [
            ("proxy", rtt, stack.fault_proxy(stack.pg_port, rtt_ms=rtt)) for rtt in args.rtts
        ]
        for kind, rtt, port in targets:
            with RttProbe(dsn_for(stack.app_password, port), interval_s=0.02) as probe:
                time.sleep(args.duration)
            row = {"path": kind, "nominal_rtt_ms": rtt, "load_average": load_average(), **probe.summary()}
            rows.append(row)
            print(json.dumps(row), flush=True)
    finally:
        stack.__exit__()
    write_json(args.out / "calibration.json", {"rows": rows})


# ---------- 왕복 수 ----------


def analyze_operation(events: list[dict], op: dict) -> dict:
    """한 쓰기의 메시지에서 왕복 수와 잠금을 쥔 동안의 왕복 수 · 시간을 센다.

    왕복 하나 = 앱이 보낸 묶음 하나 뒤에 DB 응답을 기다리는 것. 응답을 기다리지 않고 이어 보낸 메시지는 같은 묶음이다.
    """
    in_op = [e for e in events if op["started_ms"] - 1 <= e["t"] <= op["ended_ms"] + 1]
    round_trips = 0
    previous = None
    for event in in_op:
        if event["d"] == "app>db" and previous != "app>db":
            round_trips += 1
        previous = event["d"]
    statements = []
    lock_at = release_at = None
    for index, event in enumerate(in_op):
        if event["d"] != "app>db":
            continue
        for message in event["ms"]:
            sql = message.get("sql")
            if sql:
                statements.append(_statement_label(sql))
            if sql and "pg_advisory_xact_lock" in sql and lock_at is None:
                lock_at = index
            if lock_at is not None and release_at is None:
                if sql and sql.split()[0].upper() in ("COMMIT", "ROLLBACK"):
                    release_at = index
    if lock_at is not None and release_at is None:
        # COMMIT이 없으면 자동 커밋이다. 잠금 문장의 Sync에서 서버가 커밋한다.
        for index in range(lock_at, len(in_op)):
            if in_op[index]["d"] == "app>db" and any(m["m"] == "Sync" for m in in_op[index]["ms"]):
                release_at = index
                break
    hold_round_trips = hold_ms = None
    if lock_at is not None and release_at is not None:
        between = in_op[lock_at:release_at]
        hold_round_trips = sum(
            1 for x, y in zip(between, between[1:] + [in_op[release_at]], strict=False) if x["d"] == "db>app" and y["d"] == "app>db"
        )
        hold_ms = round(in_op[release_at]["t"] - in_op[lock_at]["t"], 3)
    errors = [m.get("sqlstate") for e in in_op if e["d"] == "db>app" for m in e["ms"] if m["m"] == "Error"]
    return {
        "outcome": op["outcome"],
        "elapsed_ms": op["elapsed_ms"],
        "round_trips": round_trips,
        "statements": statements,
        "hold_round_trips": hold_round_trips,
        "hold_ms": hold_ms,
        "errors": errors,
    }


def _statement_label(sql: str) -> str:
    if "pg_advisory_xact_lock" in sql and sql.lstrip().upper().startswith("WITH"):
        return "CTE(lock+insert+select)"
    if "pg_advisory_xact_lock" in sql:
        return "lock"
    if "set_config" in sql:
        return "set_config"
    return sql.split()[0].upper()


def command_wire(args) -> None:
    """앱-DB 왕복 RTT에서 쓰기 한 건마다 실제로 오간 메시지를 센다. 경쟁이 없을 때의 값이다."""
    results = {}
    for name in args.names:
        variant = args.variants / name
        log = args.out / f"wire-{name}.jsonl"
        log.unlink(missing_ok=True)
        stack = db_stack(variant, args.out / "stack")
        try:
            port = Proxies(stack).tap(stack.pg_port, log, args.rtt)
            config = {"dsn": dsn_for(stack.app_password, port, extra="?sslmode=disable&gssencmode=disable")}
            ops = driver(variant, "one-write", config)
            time.sleep(0.5)
        finally:
            stack.__exit__()
        events = [json.loads(line) for line in log.read_text().splitlines()]
        if "driver_error" in ops:
            results[name] = ops
        else:
            results[name] = {op: analyze_operation(events, value) for op, value in ops.items()}
        print(name, json.dumps(results[name].get("fresh", results[name]), ensure_ascii=False), flush=True)
        log.unlink()
    write_json(args.out / "wire.json", {"rtt_ms": args.rtt, "variants": results})


# ---------- 처리량 천장 ----------


def command_ceiling(args) -> None:
    """앱 없이 Store에 동시 쓰기를 쉬지 않고 넣는다. 잠금 줄이 항상 찬 상태의 초당 처리량이다."""
    rows = []
    for name in args.names:
        variant = args.variants / name
        stack = db_stack(variant, args.out / "stack")
        try:
            for rtt in args.rtts:
                port = stack.fault_proxy(stack.pg_port, rtt_ms=rtt)
                for tx_timeout_ms in (5000, 200):
                    config = {
                        "dsn": dsn_for(stack.app_password, port), "duration": args.duration, "warmup": 2,
                        "workers": 10, "tx_timeout_ms": tx_timeout_ms,
                    }  # fmt: skip
                    row = {"variant": name, "rtt_ms": rtt, "tx_timeout_ms": tx_timeout_ms, "load_average": load_average(), **driver(variant, "ceiling", config)}
                    rows.append(row)
                    print(json.dumps(row, ensure_ascii=False), flush=True)
                    write_json(args.out / "ceiling.json", {"rows": rows})
        finally:
            stack.__exit__()


# ---------- 불변식 ----------


def pytest_variant(variant: Path, stack: Stack) -> dict:
    """demo-chat 자체 시험을 실제 Postgres에 돌린다."""
    env = {**os.environ, "TEST_DATABASE_URL": stack.app_dsn}
    env.pop("VIRTUAL_ENV", None)
    result = subprocess.run(
        ["uv", "run", "--frozen", "pytest", "tests/test_store.py", "tests/test_api.py", "tests/test_resilience.py", "tests/test_websocket_cutover.py", "-q"],
        cwd=variant, env=env, capture_output=True, text=True, timeout=300,
    )  # fmt: skip
    return {"returncode": result.returncode, "summary": result.stdout.strip().splitlines()[-1] if result.stdout.strip() else ""}


def command_invariants(args) -> None:
    runs = []
    images = {"pg16": PG16, "pg18": PG18}
    for name in args.names:
        variant = args.variants / name
        for image_name in args.images:
            for path in args.paths:
                bouncer = path == "pgbouncer"
                stack = db_stack(variant, args.out / "stack", bouncer=bouncer, image=images[image_name])
                try:
                    target = stack.app_db_port
                    far = stack.delayed_db_port(args.rtt)
                    flaky = stack.fault_proxy(stack.pg_port, drop_chance=args.drop_chance)
                    if bouncer:
                        flaky = stack.start_bouncer("flaky", "host.docker.internal", flaky)
                    config = {
                        "dsn": dsn_for(stack.app_password, target),
                        "far_dsn": dsn_for(stack.app_password, far),
                        "flaky_dsn": dsn_for(stack.app_password, flaky),
                        "admin_dsn": stack.admin_dsn,
                        "rtt_ms": args.rtt,
                        "order_writes": args.order_writes,
                    }
                    started = time.monotonic()
                    result = driver(variant, "invariants", config)
                    run = {
                        "variant": name, "image": image_name, "path": path, "seconds": round(time.monotonic() - started, 1),
                        "load_average": load_average(), **result,
                    }  # fmt: skip
                    if not bouncer:
                        run["demo_chat_pytest"] = pytest_variant(variant, stack)
                finally:
                    stack.__exit__()
                runs.append(run)
                failed = [c for c in run.get("checks", []) if not c["ok"]]
                print(name, image_name, path, "checks", len(run.get("checks", [])), "failed", [f"{c['invariant']}: {c['check']} ({c['actual']})" for c in failed], run.get("driver_error", ""), run.get("demo_chat_pytest", ""), flush=True)
                write_json(args.out / "invariants.json", {"runs": runs})


# ---------- 지연 행렬 ----------


def app_error_counts(path: Path, offset: int) -> tuple[Counter, int]:
    """앱 로그의 503 원인을 센다. 로그는 예외 이름만 남긴다."""
    if not path.exists():
        return Counter(), offset
    data = path.read_bytes()
    counts = Counter(re.findall(r"database unavailable on (\S+): (\w+)", data[offset:].decode(errors="replace")))
    return Counter({f"{route} {name}": n for (route, name), n in counts.items()}), len(data)


def edge_error_counts(container: str, log: Path) -> dict[str, int]:
    """Edge 대역의 오류 로그를 남기고 종류별로 센다. 앱이 답하지 않은 502의 원인은 컨테이너가 지워지면 사라진다."""
    text = subprocess.run(["docker", "logs", container], capture_output=True, text=True).stderr
    log.write_text(text)
    kinds = re.findall(r"\[(?:error|crit|alert|emerg)\] \d+#\d+: (?:\*\d+ )?(.+?)(?: while |, client: )", text)
    return dict(Counter(re.sub(r"\[[0-9a-f:]+\]:\d+", "<ipv6>", re.sub(r"\d+\.\d+\.\d+\.\d+:\d+", "<ipv4>", kind)) for kind in kinds))


def summarize_cell(variant: str, rep: int, rtt: float, percent: int, path: str, run, app_errors: dict, load: list[float], pause_ms) -> dict:
    report, reconciliation, probe = run.report, run.report["reconciliation"], run.report["lock_probe"]["load"]
    verdicts = judged(run.windows, percent)
    pools = {
        pool: {
            "requests": sum(w["pools"][pool]["requests"] for w in run.windows),
            "http_5xx": sum(w["pools"][pool]["http_5xx"] for w in run.windows),
            "p95_ms_worst_window": max((w["pools"][pool]["p95_ms"] or 0) for w in run.windows),
        }
        for pool in ("source", "target")
    }
    return {
        "variant": variant, "rep": rep, "rtt_ms": rtt, "target_percent": percent, "path": path,
        "load_average_at_start": load,
        "reconcile_verdict": report["verdict"], "reconcile_reasons": report["reasons"],
        "nfr2": reconciliation["nfr2"],
        "content_mismatch": reconciliation["counts"]["content_mismatch"],
        "phantom_rows": reconciliation["counts"]["phantom_rows"],
        "logical_writes": reconciliation["counts"]["logical_writes"],
        "retried": reconciliation["counts"]["retried"],
        "write": report["requests"]["write"], "read": report["requests"]["read"],
        "write_gap_max_ms": report["write_gap"]["max_ms"],
        "achieved_write_rps": report["load"]["achieved_write_rps"],
        "achieved_read_rps": report["load"]["achieved_read_rps"],
        "scheduler_lag_ms": report["load"]["scheduler_lag_ms"],
        "warnings": report["warnings"],
        "pools": pools,
        "unattributed": {k: sum(w["unattributed"][k] for w in run.windows) for k in ("timeout", "connect", "other")},
        "judge": [v for v, _ in verdicts],
        "judge_reasons": dict(Counter(reason for _, why in verdicts for reason in why)),
        "lock_wait_ms": probe["wait_ms"], "lock_waiters_max": probe["waiting_sessions_max"], "lock_timed_out": probe["timed_out"],
        "app_errors": app_errors,
        "pause_ms": pause_ms,
    }  # fmt: skip


def run_stack_cells(args, name: str, rep: int, path: str, cells: list[tuple[float, int]], results_path: Path, *, pause_at: float | None = None) -> None:
    variant = args.variants / name
    out = args.out / "raw" / f"{name}-{path}-rep{rep}-{int(time.time())}"
    out.mkdir(parents=True)
    cls = BouncerStack if path == "pgbouncer" else Stack
    with cls(variant, out, container_prefix=PREFIX) as stack:
        harness = Harness(stack, out, 30)
        ports: dict[float, int] = {}
        offsets: dict[Path, int] = {}
        for rtt, percent in cells:
            if rtt not in ports:
                ports[rtt] = stack.delayed_target_app(rtt)
            logs = {"source": out / "app-aws.log", "target": out / f"app-delayed-{rtt:g}.log"}
            for log in logs.values():
                offsets[log] = app_error_counts(log, offsets.get(log, 0))[1]
            cell = f"rtt{rtt:g}-w{percent}"
            edge = stack.edge(cell, target_percent=percent, target_port=ports[rtt])
            load = load_average()
            pause_box: dict = {}

            def during(_process, box=pause_box, rtt=rtt):
                if pause_at is None:
                    return
                time.sleep(pause_at)
                box["ms"] = stack.pause(rtt)

            with RttProbe(dsn_for(stack.app_password, stack.delay_proxy_ports[rtt])) as rtt_probe:
                run = harness.loadgen(cell, edge, duration=args.duration, path="local-mock-edge", lock=True, during=during)
            errors = {}
            for pool, log in logs.items():
                counts, offsets[log] = app_error_counts(log, offsets[log])
                errors[pool] = dict(counts)
            row = summarize_cell(name, rep, rtt, percent, path, run, errors, load, pause_box.get("ms"))
            row["effective_rtt_ms"] = rtt_probe.summary()
            row["edge_errors"] = edge_error_counts(f"{PREFIX}-edge-{cell}-{stack.tag}", out / f"edge-{cell}.log")
            with results_path.open("a") as output:
                output.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                name, f"rep{rep}", path, cell, "judge", row["judge"], "5xx w/r", row["write"]["http_5xx"], row["read"]["http_5xx"],
                "p95 w/r", row["write"]["p95_ms"], row["read"]["p95_ms"], "nfr2", row["nfr2"], "load", load[0], "pause", row["pause_ms"], flush=True,
            )  # fmt: skip


def command_matrix(args) -> None:
    """같은 조건의 칸을 개선안마다 돈다. 다른 작업의 영향이 한쪽에 몰리지 않게 개선안을 번갈아 돈다."""
    cells = [(rtt, percent) for rtt in args.rtts for percent in args.percents]
    results_path = args.out / f"{args.label}.jsonl"
    for rep in range(args.first_rep, args.first_rep + args.reps):
        for name in args.names:
            run_stack_cells(args, name, rep, args.path, cells, results_path, pause_at=args.pause_at)


# ---------- 공통 ----------


def load_average() -> list[float]:
    return [round(value, 2) for value in os.getloadavg()]


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["calibrate", "wire", "ceiling", "invariants", "matrix"])
    parser.add_argument("--variants", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--names", nargs="+", required=True)
    parser.add_argument("--rtt", type=float, default=20)
    parser.add_argument("--rtts", nargs="+", type=float, default=[0, 5, 20, 40])
    parser.add_argument("--percents", nargs="+", type=int, default=[10, 50])
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--reps", type=int, default=1)
    parser.add_argument("--first-rep", type=int, default=1)
    parser.add_argument("--label", default="matrix")
    parser.add_argument("--path", choices=["direct", "pgbouncer"], default="direct")
    parser.add_argument("--paths", nargs="+", default=["direct", "pgbouncer"])
    parser.add_argument("--images", nargs="+", default=["pg16", "pg18"])
    parser.add_argument("--order-writes", type=int, default=600)
    parser.add_argument("--drop-chance", type=float, default=0.08)
    parser.add_argument("--pause-at", type=float)
    args = parser.parse_args()
    args.variants = args.variants.resolve()
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    {"calibrate": command_calibrate, "wire": command_wire, "ceiling": command_ceiling, "invariants": command_invariants, "matrix": command_matrix}[args.command](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
