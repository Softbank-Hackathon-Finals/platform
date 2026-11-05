#!/usr/bin/env python3
"""T18 로컬 종단 시험. 실제 demo-chat + 실제 Postgres에 부하를 넣고, 일부러 망가뜨려 잡히는지 본다.

    uv run python testing/e2e.py --demo-chat <demo-chat 폴더> --out <산출물 폴더>

- Postgres 이미지와 Edge 대역(nginx) 이미지는 이미 받은 로컬 이미지를 쓴다. 없으면 받지 않고 멈춘다.
- 비밀번호 · 토큰은 실행마다 새로 만들어 환경변수로만 넘긴다. 파일에 남기지 않는다.
- 실제 Cloudflare · 실제 클러스터가 아니다. 결과는 로컬 실측이다.
"""

import argparse
import json
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import psutil
import psycopg
import psycopg.sql
from bravo_observability import judge, receive

from bravo_loadgen.lock_probe import MESSAGE_WRITE_LOCK
from bravo_loadgen.schema import SchemaViolation, validate_window

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
PROFILE = json.loads((REPO / "docs/observability/threshold-profiles.json").read_text())["profiles"][0]
POSTGRES_IMAGE = "public.ecr.aws/docker/library/postgres:16"
EDGE_IMAGE = "nginx:alpine"
MOTO_VERSION = "5.1.4"  # demo-chat compose.yaml과 같은 버전
SOURCE_ENV, TARGET_ENV = "aws", "onprem"
WINDOW_TOKEN = "t18-e2e-window-token"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_quiet(*command: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=True, capture_output=True, text=True, **kwargs)


def wait_until(ready: Callable[[], bool], what: str, seconds: float = 60) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            if ready():
                return
        except (OSError, httpx.HTTPError, psycopg.Error):
            pass
        time.sleep(0.25)
    raise TimeoutError(f"{what}가 {seconds}초 안에 준비되지 않았다")


class Stack:
    """Postgres · moto · 앱 둘 · Edge 대역을 띄우고 끝나면 지운다."""

    def __init__(self, demo_chat: Path, out: Path, container_prefix: str = "bravo-t18", postgres_image: str = POSTGRES_IMAGE):
        self.demo = demo_chat
        self.out = out
        self.tag = uuid.uuid4().hex[:8]
        self.container_prefix = container_prefix
        self.postgres_image = postgres_image
        self.admin_password = secrets.token_urlsafe(16)
        self.app_password = secrets.token_urlsafe(16)
        self.admin_token = secrets.token_urlsafe(16)
        self.pg_port = free_port()
        self.app_db_port = self.pg_port  # 앱이 붙는 포트. 풀러를 앞에 둘 때 바꾼다
        self.pg_name = f"{container_prefix}-pg-{self.tag}"
        self.source_port = free_port()
        self.target_port = free_port()
        self.processes: list[subprocess.Popen] = []
        self.containers: list[str] = []
        self.edges: dict[str, str] = {}
        self.delay_proxy_ports: dict[float, int] = {}

    def __enter__(self):
        try:
            self._require_same_lock_key()
            self._require_local_images()
            self._start_postgres()
            self._start_moto()
            self._start_apps()
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc_info):
        for process in self.processes:
            process.terminate()
        for process in self.processes:
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
        for name in self.containers:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)

    @property
    def admin_dsn(self) -> str:
        return f"postgresql://postgres:{self.admin_password}@127.0.0.1:{self.pg_port}/chat"

    @property
    def app_dsn(self) -> str:
        return f"postgresql://chat_app:{self.app_password}@127.0.0.1:{self.pg_port}/chat"

    @property
    def source_url(self) -> str:
        return f"http://127.0.0.1:{self.source_port}"

    @property
    def target_url(self) -> str:
        return f"http://127.0.0.1:{self.target_port}"

    def _require_same_lock_key(self) -> None:
        """앱의 잠금 키가 바뀌면 측정기는 빈 잠금을 재서 0에 가까운 값을 정상처럼 낸다."""
        found = re.search(r"^MESSAGE_WRITE_LOCK = ([\d_]+)", (self.demo / "app/store.py").read_text(), re.M)
        if found is None or int(found.group(1)) != MESSAGE_WRITE_LOCK:
            raise SystemExit("demo-chat의 쓰기 잠금 키가 lock_probe.py와 다르다")

    def _require_local_images(self) -> None:
        for image in (self.postgres_image, EDGE_IMAGE):
            found = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
            if found.returncode != 0:
                raise SystemExit(f"로컬에 이미지가 없다: {image}. 자격 증명을 건드리지 않고 멈춘다.")

    def _start_postgres(self) -> None:
        run_quiet(
            "docker", "run", "-d", "--rm", "--name", self.pg_name,
            "-e", "POSTGRES_DB=chat", "-e", f"POSTGRES_PASSWORD={self.admin_password}",
            "-p", f"127.0.0.1:{self.pg_port}:5432", self.postgres_image,
            "postgres", "-c", "wal_level=logical", "-c", "max_connections=200",
        )  # fmt: skip
        self.containers.append(self.pg_name)

        def ready() -> bool:
            with psycopg.connect(self.admin_dsn, connect_timeout=2) as connection:
                connection.execute("SELECT 1")
            return True

        wait_until(ready, "Postgres")
        with psycopg.connect(self.admin_dsn, autocommit=True) as connection:
            connection.execute(
                psycopg.sql.SQL("CREATE ROLE chat_app LOGIN PASSWORD {}").format(
                    psycopg.sql.Literal(self.app_password)
                )
            )
            connection.execute((self.demo / "db/schema.sql").read_text())

    def _app_env(self, app_env: str) -> dict[str, str]:
        return {
            **os.environ,
            "APP_ENV": app_env,
            "DB_HOST": "127.0.0.1",
            "DB_PORT": str(self.app_db_port),
            "DB_NAME": "chat",
            "DB_SECRET_ID": "demo-chat/db",
            "S3_BUCKET": "demo-chat-attachments",
            "S3_ENDPOINT_URL": self.moto_url,
            "S3_FORCE_PATH_STYLE": "true",
            "AWS_ENDPOINT_URL_SECRETS_MANAGER": self.moto_url,
            "AWS_DEFAULT_REGION": "ap-northeast-1",
            "AWS_ACCESS_KEY_ID": "local",
            "AWS_SECRET_ACCESS_KEY": "local",
            "ADMIN_TOKEN": self.admin_token,
        }

    def _start_moto(self) -> None:
        port = free_port()
        self.moto_url = f"http://127.0.0.1:{port}"
        log = (self.out / "moto.log").open("w")
        self.processes.append(
            subprocess.Popen(
                ["uvx", "--from", f"moto[server]=={MOTO_VERSION}", "moto_server", "-H", "127.0.0.1", "-p", str(port)],
                stdout=log, stderr=log,
            )  # fmt: skip
        )
        wait_until(lambda: httpx.get(self.moto_url, trust_env=False).status_code < 500, "moto", 180)

    def _start_apps(self) -> None:
        run_quiet("uv", "sync", "--frozen", "--no-dev", "--quiet", cwd=self.demo)
        seed = (
            "import boto3,json,os\n"
            "boto3.client('s3').create_bucket(Bucket='demo-chat-attachments',"
            "CreateBucketConfiguration={'LocationConstraint':'ap-northeast-1'})\n"
            "boto3.client('secretsmanager').create_secret(Name='demo-chat/db',"
            "SecretString=json.dumps({'username':'chat_app','password':os.environ['APP_DB_PASSWORD']}))\n"
        )
        env = {
            **self._app_env("seed"),
            "AWS_ENDPOINT_URL": self.moto_url,
            "APP_DB_PASSWORD": self.app_password,
        }
        run_quiet(str(self.demo / ".venv/bin/python"), "-c", seed, env=env, cwd=self.demo)
        for name, port in ((SOURCE_ENV, self.source_port), (TARGET_ENV, self.target_port)):
            log = (self.out / f"app-{name}.log").open("w")
            self.processes.append(
                subprocess.Popen(
                    [str(self.demo / ".venv/bin/uvicorn"), "app.main:create_app", "--factory",
                     "--host", "127.0.0.1", "--port", str(port), "--no-access-log", "--log-level", "warning"],
                    cwd=self.demo, env=self._app_env(name), stdout=log, stderr=log,
                )  # fmt: skip
            )
            url = f"http://127.0.0.1:{port}/api/health/db"
            wait_until(lambda u=url: httpx.get(u, trust_env=False).status_code == 200, f"앱 {name}")

    def fault_proxy(self, target_port: int, *, rtt_ms: float = 0, drop_every: int = 0, drop_chance: float = 0) -> int:
        port = free_port()
        self.processes.append(
            subprocess.Popen(
                [sys.executable, str(HERE / "tcp_fault.py"), str(port), str(target_port),
                 "--rtt-ms", str(rtt_ms), "--drop-every", str(drop_every), "--drop-chance", str(drop_chance)]
            )  # fmt: skip
        )
        wait_until(lambda: socket.create_connection(("127.0.0.1", port), 1).close() is None, "결함 중계기")
        return port

    def delayed_db_port(self, rtt_ms: float) -> int:
        """왕복 지연을 지나 DB에 닿는 포트. 지연 앱이 여기에 붙는다."""
        proxy_port = self.fault_proxy(self.pg_port, rtt_ms=rtt_ms)
        self.delay_proxy_ports[rtt_ms] = proxy_port
        return proxy_port

    def delayed_target_app(self, rtt_ms: float) -> int:
        """DB까지 왕복 지연이 있는 목적지 앱을 띄운다. 트래픽 전환 중 목적지 앱이 출발지 DB에 쓰는 경로의 대역이다."""
        db_port = self.delayed_db_port(rtt_ms)
        port = free_port()
        log = (self.out / f"app-delayed-{rtt_ms:g}.log").open("w")
        env = {**self._app_env(TARGET_ENV), "DB_PORT": str(db_port)}
        self.processes.append(
            subprocess.Popen(
                [str(self.demo / ".venv/bin/uvicorn"), "app.main:create_app", "--factory",
                 "--host", "127.0.0.1", "--port", str(port), "--no-access-log", "--log-level", "warning"],
                cwd=self.demo, env=env, stdout=log, stderr=log,
            )  # fmt: skip
        )
        url = f"http://127.0.0.1:{port}/api/health/db"
        wait_until(lambda: httpx.get(url, trust_env=False).status_code == 200, "지연 앱")
        return port

    def _edge_conf_file(self, name: str, target_percent: int, target_port: int | None) -> Path:
        conf = (HERE / "mock-edge.conf").read_text()
        if target_percent == 0:
            # nginx split_clients는 0%를 받지 않는다. 목적지 줄을 빼면 전부 출발지로 간다.
            conf = conf.replace("    __TARGET_PERCENT__% target;\n", "")
        for placeholder, value in (
            ("__SOURCE_PORT__", self.source_port),
            ("__TARGET_PORT__", target_port or self.target_port),
            ("__TARGET_PERCENT__", target_percent),
        ):
            conf = conf.replace(placeholder, str(value))
        path = self.out / f"edge-{name}.conf"
        path.write_text(conf)
        return path

    def set_edge_weight(self, name: str, target_percent: int) -> None:
        """대역의 가중치를 실행 중에 바꾼다. 실제 LB 가중치 변경의 대역이고, 반영 시간은 실제 값이 아니다."""
        container = f"{self.container_prefix}-edge-{name}-{self.tag}"
        path = self._edge_conf_file(name, target_percent, None)
        run_quiet("docker", "cp", str(path), f"{container}:/etc/nginx/conf.d/default.conf")
        run_quiet("docker", "exec", container, "nginx", "-s", "reload")

    def edge(self, name: str, *, target_percent: int, target_port: int | None = None) -> str:
        """가중치를 정한 Edge 대역을 띄운다. target_port를 닫힌 포트로 주면 Edge 오류를 만든다."""
        if name in self.edges:
            return self.edges[name]
        port = free_port()
        container = f"{self.container_prefix}-edge-{name}-{self.tag}"
        path = self._edge_conf_file(name, target_percent, target_port)
        run_quiet(
            "docker", "create", "--name", container, "--add-host=host.docker.internal:host-gateway",
            "-p", f"127.0.0.1:{port}:8080", EDGE_IMAGE,
        )  # fmt: skip
        self.containers.append(container)
        run_quiet("docker", "cp", str(path), f"{container}:/etc/nginx/conf.d/default.conf")
        run_quiet("docker", "start", container)
        url = f"http://127.0.0.1:{port}"
        wait_until(lambda: httpx.get(f"{url}/healthz", trust_env=False).status_code == 200, f"Edge {name}")
        self.edges[name] = url
        return url

    def set_write_block(self, blocked: bool, urls: tuple[str, ...] | None = None) -> None:
        for url in urls or (self.source_url, self.target_url):
            response = httpx.put(
                f"{url}/admin/write-block",
                json={"blocked": blocked},
                headers={"Authorization": f"Bearer {self.admin_token}"},
                trust_env=False,
            )
            response.raise_for_status()

    def admin_execute(self, sql: str, params=None) -> list[tuple]:
        with psycopg.connect(self.admin_dsn, autocommit=True) as connection:
            cursor = connection.execute(sql, params)
            return cursor.fetchall() if cursor.description else []


@dataclass
class Run:
    out: Path
    code: int
    report: dict
    windows: list[dict]


def sample_resources(process: subprocess.Popen, samples: list[tuple[float, float, int]]) -> None:
    """부하 생성기 프로세스를 밖에서 1초마다 읽는다: (시각, 누적 CPU 초, RSS 바이트)."""
    watched = psutil.Process(process.pid)
    while process.poll() is None:
        try:
            times = watched.cpu_times()
            samples.append((time.time(), times.user + times.system, watched.memory_info().rss))
        except psutil.Error:
            return
        time.sleep(1)


def summarize_resources(samples: list[tuple[float, float, int]], start_wall: str, duration: float) -> dict:
    """부하를 넣는 구간만 따로 센다. 시작 · 대조 단계까지 포함한 RSS 최댓값은 따로 둔다."""
    start = datetime.fromisoformat(start_wall).timestamp()
    load = [s for s in samples if start <= s[0] <= start + duration]
    rates = [(b[1] - a[1]) / (b[0] - a[0]) for a, b in zip(load, load[1:], strict=False)]
    mib = [s[2] / 2**20 for s in load]
    return {
        "samples": len(load),
        "cpu_millicores": {
            "avg": round((load[-1][1] - load[0][1]) / (load[-1][0] - load[0][0]) * 1000),
            "max_1s": round(max(rates) * 1000),
        },
        "rss_mib": {
            "avg": round(sum(mib) / len(mib), 1),
            "max": round(max(mib), 1),
            "first_10_samples_avg": round(sum(mib[:10]) / len(mib[:10]), 1),
            "last_10_samples_avg": round(sum(mib[-10:]) / len(mib[-10:]), 1),
        },
        "rss_mib_whole_run_max": round(max(s[2] for s in samples) / 2**20, 1),
    }


class Harness:
    def __init__(self, stack: Stack, out: Path, duration: float):
        self.stack = stack
        self.out = out
        self.duration = duration
        self.checks: list[dict] = []
        # 측정 시작 때 이 컴퓨터의 부하. 다른 작업이 값을 흔들었는지 읽을 근거다.
        self.load_average: dict[str, list[float]] = {}
        self.resources: dict[str, dict] = {}

    def check(self, scenario: str, name: str, ok: bool, actual) -> None:
        self.checks.append({"scenario": scenario, "check": name, "ok": bool(ok), "actual": actual})
        print(f"  {'ok  ' if ok else 'FAIL'} {name} (실제: {actual})", flush=True)

    def _env(self) -> dict[str, str]:
        return {
            **os.environ,
            "LOADGEN_DB_DSN": self.stack.app_dsn,
            "LOADGEN_LOCK_DSN": self.stack.app_dsn,
            "BRAVO_WINDOW_TOKEN": WINDOW_TOKEN,
        }

    def loadgen(
        self,
        name: str,
        base_url: str,
        *,
        duration: float,
        write: float = 50,
        read: float = 150,
        window: int = 10,
        path: str = "local-direct",
        lock: bool = False,
        extra: tuple[str, ...] = (),
        during: Callable[[subprocess.Popen], None] | None = None,
        subcommand: str = "run",
    ) -> Run:
        out = self.out / name
        out.mkdir(parents=True, exist_ok=True)
        self.load_average[name] = [round(value, 2) for value in os.getloadavg()]
        command = [
            sys.executable, "-m", "bravo_loadgen.cli", subcommand, "--base-url", base_url,
            "--plan-id", "plan-local", "--source-env", SOURCE_ENV, "--target-env", TARGET_ENV,
            "--duration", str(duration), "--write-rps", str(write), "--read-rps", str(read),
            "--window-seconds", str(window), "--measurement-path", path, "--out", str(out),
            "--run-id", f"t18-{name}-{uuid.uuid4().hex[:6]}"[:32].rstrip("-"),
            "--db-dsn-env", "LOADGEN_DB_DSN", *extra,
        ]  # fmt: skip
        if lock:
            command += ["--lock-dsn-env", "LOADGEN_LOCK_DSN"]
        with (out / "stdout.log").open("w") as log:
            process = subprocess.Popen(command, env=self._env(), stdout=log, stderr=subprocess.STDOUT)
            samples: list[tuple[float, float, int]] = []
            sampler = threading.Thread(target=sample_resources, args=(process, samples))
            sampler.start()
            if during:
                during(process)
            code = process.wait()
            sampler.join()
        run = self._load_run(out, code)
        self.resources[name] = summarize_resources(samples, run.report["started_at"], duration)
        return run

    def reconcile(self, out: Path) -> Run:
        with (out / "stdout.log").open("a") as log:
            code = subprocess.run(
                [sys.executable, "-m", "bravo_loadgen.cli", "reconcile", "--out", str(out),
                 "--db-dsn-env", "LOADGEN_DB_DSN"],
                env=self._env(), stdout=log, stderr=subprocess.STDOUT,
            ).returncode  # fmt: skip
        return self._load_run(out, code)

    @staticmethod
    def _load_run(out: Path, code: int) -> Run:
        return Run(
            out=out,
            code=code,
            report=json.loads((out / "report.json").read_text()),
            windows=[json.loads(line) for line in (out / "windows.jsonl").read_text().splitlines()],
        )

    def check_windows_command(self, path: Path, window: int = 10) -> tuple[int, str]:
        result = subprocess.run(
            [sys.executable, "-m", "bravo_loadgen.cli", "check-windows", str(path),
             "--window-seconds", str(window)],
            capture_output=True, text=True,
        )  # fmt: skip
        return result.returncode, result.stdout.strip().splitlines()[-1] if result.stdout else ""


def judged(windows: list[dict], weight: int) -> list[tuple[str, list[str]]]:
    """T36 기준 구현으로 창마다 판정한다. 각 창이 끝난 1초 뒤 받아 2초 뒤 판정한 것으로 친다."""
    verdicts = []
    for window in windows:
        end = datetime.fromisoformat(window["window"]["end"])
        received = receive(window, (end + timedelta(seconds=1)).isoformat())
        result = judge(
            PROFILE,
            received,
            plan_id=window["plan_id"],
            state="SHIFT",
            weight_target=weight,
            observe_from=window["window"]["start"],
            decided_at=(end + timedelta(seconds=2)).isoformat(),
            prior_unknown=0,
        )
        verdicts.append((result["verdict"], result["reasons"]))
    return verdicts


def pool_total(window: dict) -> int:
    return window["pools"]["source"]["requests"] + window["pools"]["target"]["requests"]


def window_total(window: dict) -> int:
    return pool_total(window) + sum(window["unattributed"].values())


def scenario_direct(h: Harness) -> None:
    """주 측정 1: 수정하지 않은 demo-chat에 직접. 앱이 붙인 헤더로 전부 출발지에 세어진다."""
    # 다른 작업이 값을 흔들 수 있어 세 번 돈다. 고르지 않고 셋 다 남긴다.
    for name in ("direct", "direct-2", "direct-3"):
        print(f"== {name}: 50 + 150 req/s, demo-chat 직접", flush=True)
        r = h.loadgen(name, h.stack.source_url, duration=h.duration, lock=True)
        rep, rec = r.report, r.report["reconciliation"]
        h.check(name, "대조 판정 PASS, 종료 코드 0", r.code == 0 and rep["verdict"] == "PASS", rep["verdict"])
        h.check(name, "실패 · 유실 · 중복 0", rec["nfr2"] == {"failed": 0, "lost": 0, "duplicated": 0}, rec["nfr2"])
        h.check(name, "쓰기 50 req/s 달성 (±1%)", abs(rep["load"]["achieved_write_rps"] - 50) <= 0.5, rep["load"]["achieved_write_rps"])
        h.check(name, "읽기 150 req/s 달성 (±1%)", abs(rep["load"]["achieved_read_rps"] - 150) <= 1.5, rep["load"]["achieved_read_rps"])
        h.check(name, "모든 응답이 출발지로 세어짐, 환경 미확인 0",
                not {"ENV_HEADER_MISSING", "ENV_HEADER_UNREGISTERED"} & set(rep["warnings"])
                and rep["windows"]["pools_total"] == {"source": rep["windows"]["requests_in_windows"], "target": 0}
                and sum(rep["windows"]["unattributed_total"].values()) == 0 and rep["contract_error_rate"] == 0,
                rep["env_header"])  # fmt: skip
        h.check(name, "판정 입력으로 쓸 수 없다고 표시", rep["sli_usable_for_judgement"] is False, rep["warnings"])
        h.check(name, "창 집계가 전체 시도와 일치", rep["windows"]["conserved"], rep["windows"]["requests_in_windows"])
        h.check(name, "잠금 대기를 부하 중에 측정", rep["lock_probe"]["load"]["samples"] > 0, rep["lock_probe"]["load"])
        code, line = h.check_windows_command(r.out / "windows.jsonl")
        h.check(name, "창 파일이 스키마 · 연속성 통과", code == 0, line)
        verdicts = judged(r.windows, 0)
        h.check(name, "기준 구현 judge가 가중치 0%에서 PASS로 읽음", all(v == "PASS" for v, _ in verdicts), [v for v, _ in verdicts])
        verdicts = judged(r.windows, 10)
        h.check(name, "가중치 10%로 읽으면 목적지 표본이 없어 UNKNOWN (PASS로 읽지 않음)", all(v == "UNKNOWN" and "NOT_APPLIED" in why for v, why in verdicts), verdicts[0])


def scenario_mock_edge(h: Harness) -> None:
    """주 측정 2: Edge 대역(10%)으로 환경별 집계를 본다. 헤더는 앱이 붙이고 대역은 나누기만 한다."""
    print("== mock-edge: 10% 단계, Edge 대역", flush=True)
    edge = h.stack.edge("w10", target_percent=10)
    r = h.loadgen("mock-edge-10", edge, duration=h.duration, path="local-mock-edge", lock=True)
    rep, rec = r.report, r.report["reconciliation"]
    h.check("mock-edge", "대조 판정 PASS", r.code == 0, rep["verdict"])
    h.check("mock-edge", "실패 · 유실 · 중복 0", rec["nfr2"] == {"failed": 0, "lost": 0, "duplicated": 0}, rec["nfr2"])
    h.check("mock-edge", "환경 미확인 0", sum(rep["windows"]["unattributed_total"].values()) == 0, rep["windows"]["unattributed_total"])
    h.check("mock-edge", "응답마다 앱 헤더 하나, 등록된 환경 값만", rep["env_header"]["responses_without_header"] == 0 and not rep["env_header"]["unregistered_values"] and rep["contract_error_rate"] == 0, rep["env_header"])
    samples = [window_total(w) for w in r.windows]
    h.check("mock-edge", f"창마다 전체 표본 ≥ {PROFILE['min_requests']}", all(s >= PROFILE["min_requests"] for s in samples), samples)
    targets = [w["pools"]["target"]["requests"] for w in r.windows]
    h.check("mock-edge", f"창마다 목적지 표본 ≥ {PROFILE['min_pool_requests']}", all(t >= PROFILE["min_pool_requests"] for t in targets), targets)
    # 환경을 하나도 못 읽은 창은 비율이 없다. 시험을 죽이지 않고 실패로 남긴다.
    shares = [round(w["pools"]["target"]["requests"] / pool_total(w), 3) if pool_total(w) else None for w in r.windows]
    h.check("mock-edge", f"목적지 비율 10% ± {PROFILE['share_tolerance']}", all(s is not None and abs(s - 0.10) <= PROFILE["share_tolerance"] for s in shares), shares)
    verdicts = judged(r.windows, 10)
    h.check("mock-edge", "창 전부 기준 구현 judge가 PASS로 읽음 (가중치 10%)", all(v == "PASS" for v, _ in verdicts), [v for v, _ in verdicts])
    h.check("mock-edge", "Edge 측정이 아니라고 표시", "NOT_EDGE_MEASURED" in rep["warnings"] and not rep["measurement"]["edge_measured"], rep["measurement"])
    code, line = h.check_windows_command(r.out / "windows.jsonl")
    h.check("mock-edge", "창 파일이 스키마 · 연속성 통과", code == 0, line)


def scenario_write_block(h: Harness) -> None:
    """쓰기 차단 5초(컷오버의 쓰기 정지와 같은 수단): 재시도로 모두 확인되고 공백이 잡힌다."""
    print("== write-block: 부하 중 쓰기 차단 5초", flush=True)
    edge = h.stack.edge("w10", target_percent=10)

    def during(_process):
        time.sleep(12)
        h.stack.set_write_block(True)
        time.sleep(5)
        h.stack.set_write_block(False)

    r = h.loadgen("write-block", edge, duration=30, path="local-mock-edge", during=during)
    rep, rec = r.report, r.report["reconciliation"]
    h.check("write-block", "재시도로 복구되어 대조 PASS", r.code == 0, rep["verdict"])
    h.check("write-block", "재시도가 실제로 일어남", rec["counts"]["retried"] > 0, rec["counts"]["retried"])
    h.check("write-block", "실패 · 유실 · 중복 0", rec["nfr2"] == {"failed": 0, "lost": 0, "duplicated": 0}, rec["nfr2"])
    h.check("write-block", "쓰기 응답 최대 간격이 차단 시간 이상 (재는 값: 5초 차단)", rep["write_gap"]["max_ms"] >= 4900, rep["write_gap"])
    errors = sum(w["pools"]["source"]["http_5xx"] + w["pools"]["target"]["http_5xx"] for w in r.windows)
    h.check("write-block", "창에 503이 5xx로 세어짐", errors > 0, errors)


def scenario_ack_loss(h: Harness) -> None:
    """응답만 유실: 앱이 커밋한 뒤 응답을 3개 중 1개꼴로 끊는다. 같은 키 재시도로 복구되고 중복이 없어야 한다."""
    print("== ack-loss: 커밋 뒤 응답 유실", flush=True)
    proxy = h.stack.fault_proxy(h.stack.source_port, drop_every=3)
    r = h.loadgen("ack-loss", f"http://127.0.0.1:{proxy}", duration=12, write=20, read=0)
    rep, rec = r.report, r.report["reconciliation"]
    h.check("ack-loss", "연결이 실제로 끊김", rep["requests"]["write"]["connect"] > 0, rep["requests"]["write"]["connect"])
    h.check("ack-loss", "같은 키 재시도가 저장된 메시지를 돌려받음(200)", rec["counts"]["idempotent_replays"] > 0, rec["counts"]["idempotent_replays"])
    h.check("ack-loss", "전부 확인되어 대조 PASS", r.code == 0 and rec["counts"]["confirmed"] == rec["counts"]["logical_writes"], rec["counts"]["confirmed"])
    h.check("ack-loss", "실패 · 유실 · 중복 0", rec["nfr2"] == {"failed": 0, "lost": 0, "duplicated": 0}, rec["nfr2"])


def scenario_failed_but_stored(h: Harness) -> None:
    """응답이 전부 끊겨 한도 안에 확인을 못 받았지만 저장된 경우: 유실이 아니라 실패로 세고 FAIL이어야 한다."""
    print("== failed-but-stored: 저장됐지만 끝내 응답 없음", flush=True)
    proxy = h.stack.fault_proxy(h.stack.source_port, drop_every=1)
    r = h.loadgen(
        "failed-but-stored", f"http://127.0.0.1:{proxy}", duration=11, write=10, read=0,
        extra=("--retry-budget", "1.2"),
    )  # fmt: skip
    rep, rec = r.report, r.report["reconciliation"]
    h.check("failed-but-stored", "FAIL, 종료 코드 1", r.code == 1 and rep["verdict"] == "FAIL", rep["reasons"])
    h.check("failed-but-stored", "전부 응답 없음 + 저장됨으로 셈", rec["counts"]["failed_but_stored"] == rec["counts"]["logical_writes"] > 0, rec["counts"]["failed_but_stored"])
    h.check("failed-but-stored", "유실 · 중복으로 잘못 세지 않음", rec["nfr2"]["lost"] == 0 and rec["nfr2"]["duplicated"] == 0, rec["nfr2"])


def scenario_tampering(h: Harness) -> None:
    """DB를 직접 고쳐 유실 · 중복 · 변조 · 유령을 넣고, 원상 복구하면 다시 통과하는지 본다."""
    print("== tampering: 끝난 실행의 DB를 고쳐 대조", flush=True)
    s = h.stack
    r = h.loadgen("tamper", s.source_url, duration=11, write=20, read=10)
    h.check("tampering", "변조 전에는 PASS", r.code == 0, r.report["verdict"])
    run_id = r.report["run_id"]
    author = f"loadgen-{run_id}"

    def rec(run):
        return run.report["reconciliation"]

    rows = s.admin_execute(
        "SELECT id, idempotency_key, author, body, created_at FROM messages WHERE author = %s ORDER BY id LIMIT 3",
        (author,),
    )
    s.admin_execute("DELETE FROM messages WHERE id = ANY(%s)", ([row[0] for row in rows],))
    lost = h.reconcile(r.out)
    h.check("tampering", "행 3개 삭제 → 유실 3, FAIL, 종료 1", lost.code == 1 and rec(lost)["nfr2"]["lost"] == 3, rec(lost)["nfr2"])
    for row in rows:
        s.admin_execute(
            "INSERT INTO messages (id, idempotency_key, author, body, created_at) VALUES (%s, %s, %s, %s, %s)", row
        )
    restored = h.reconcile(r.out)
    h.check("tampering", "복구하면 다시 PASS", restored.code == 0, restored.report["verdict"])

    copies = [str(uuid.uuid4()), str(uuid.uuid4())]
    for key, row in zip(copies, rows, strict=False):
        s.admin_execute("INSERT INTO messages (idempotency_key, author, body) VALUES (%s, %s, %s)", (key, row[2], row[3]))
    duplicated = h.reconcile(r.out)
    h.check("tampering", "다른 키로 복사 2건 → 중복 2, FAIL", duplicated.code == 1 and rec(duplicated)["nfr2"]["duplicated"] == 2, rec(duplicated)["nfr2"])
    s.admin_execute("DELETE FROM messages WHERE idempotency_key = ANY(%s::uuid[])", (copies,))

    s.admin_execute("UPDATE messages SET body = 'tampered' WHERE id = %s", (rows[0][0],))
    changed = h.reconcile(r.out)
    h.check("tampering", "본문 변조 1건 → 내용 불일치 1, FAIL", changed.code == 1 and rec(changed)["counts"]["content_mismatch"] == 1, rec(changed)["reasons"])
    s.admin_execute("UPDATE messages SET body = %s WHERE id = %s", (rows[0][3], rows[0][0]))

    stray = str(uuid.uuid4())
    s.admin_execute(
        "INSERT INTO messages (idempotency_key, author, body) VALUES (%s, %s, %s)",
        (stray, author, f"t18 run={run_id} req={uuid.uuid4()}"),
    )
    phantom = h.reconcile(r.out)
    h.check("tampering", "요청한 적 없는 행 1건 → 유령 1, FAIL", phantom.code == 1 and rec(phantom)["counts"]["phantom_rows"] == 1, rec(phantom)["reasons"])
    s.admin_execute("DELETE FROM messages WHERE idempotency_key = %s", (stray,))
    # 변조 시험 뒤 상태를 끝까지 되돌렸는지가 위 판정들의 전제다.
    final = h.reconcile(r.out)
    h.check("tampering", "모두 되돌리면 PASS", final.code == 0, final.report["reasons"])


def scenario_all_failed(h: Harness) -> None:
    """쓰기 차단이 재시도 한도보다 오래가면 전부 실패다. 숨기지 않는다."""
    print("== all-failed: 끝까지 쓰기 차단", flush=True)
    h.stack.set_write_block(True)
    try:
        r = h.loadgen(
            "all-failed", h.stack.source_url, duration=11, write=10, read=10,
            extra=("--retry-budget", "1"),
        )  # fmt: skip
    finally:
        h.stack.set_write_block(False)
    rep, rec = r.report, r.report["reconciliation"]
    h.check("all-failed", "FAIL, 종료 코드 1", r.code == 1, rep["verdict"])
    h.check("all-failed", "논리 쓰기 전부 실패로 셈", rec["counts"]["failed_not_stored"] == rec["counts"]["logical_writes"] > 0, rec["counts"]["failed_not_stored"])
    h.check("all-failed", "성공 응답이 없으면 간격이 부하 전체", rep["write_gap"]["position"] == "no_ack", rep["write_gap"])


def scenario_edge_error(h: Harness) -> None:
    """목적지가 죽어 Edge가 직접 오류를 낸다: 헤더 없는 502는 환경 미확인 오류, 쓰기는 재시도로 살아남는다."""
    print("== edge-error: 목적지 연결 불가, Edge 오류 페이지", flush=True)
    edge = h.stack.edge("dead-target", target_percent=10, target_port=free_port())
    r = h.loadgen("edge-error", edge, duration=21, path="local-mock-edge")
    rep, rec = r.report, r.report["reconciliation"]
    other = rep["windows"]["unattributed_total"]["other"]
    h.check("edge-error", "헤더 없는 502가 환경 미확인 other로 세어짐", other > 0, other)
    h.check("edge-error", "목적지 표본 0", rep["windows"]["pools_total"]["target"] == 0, rep["windows"]["pools_total"])
    share = other / rep["windows"]["requests_in_windows"]
    h.check("edge-error", "계약 오류율이 기준(1%)을 넘음 → 판정이 FAIL로 읽을 값", share > PROFILE["error_rate_max"], round(share, 4))
    verdicts = judged(r.windows, 10)
    h.check("edge-error", "기준 구현 judge가 오류율 FAIL로 읽음", all(v == "FAIL" and "ERROR_RATE" in why for v, why in verdicts), verdicts[0])
    h.check("edge-error", "쓰기는 재시도로 복구, 실패 · 유실 · 중복 0", rec["nfr2"] == {"failed": 0, "lost": 0, "duplicated": 0}, rec["nfr2"])


def scenario_db_pause(h: Harness) -> None:
    """DB를 5초 멈춘다(PgBouncer PAUSE와 비슷한 상황): 유실 · 중복 없이 이어지고 공백이 잡힌다."""
    print("== db-pause: 부하 중 DB 5초 정지", flush=True)

    def during(_process):
        time.sleep(15)
        run_quiet("docker", "pause", h.stack.pg_name)
        time.sleep(5)
        run_quiet("docker", "unpause", h.stack.pg_name)

    r = h.loadgen("db-pause", h.stack.source_url, duration=30, lock=True, during=during, extra=("--lock-baseline", "3"))
    rep, rec = r.report, r.report["reconciliation"]
    h.check("db-pause", "유실 · 중복 0", rec["nfr2"]["lost"] == 0 and rec["nfr2"]["duplicated"] == 0, rec["nfr2"])
    h.check("db-pause", "쓰기 응답 최대 간격이 정지 시간에 가까움", rep["write_gap"]["max_ms"] >= 4500, rep["write_gap"])
    probe = rep["lock_probe"]["load"]
    h.check("db-pause", "DB 정지가 잠금 측정에 멈춤으로 드러남", "LOCK_PROBE_STALLED" in rep["warnings"] and probe["round_trip_ms"]["max"] >= 3000, probe)
    h.check("db-pause", "측정기가 정지 뒤에도 표본을 이어 감 (0.2초 간격 × 22초의 절반 이상)", probe["samples"] >= 55, probe["samples"])


def scenario_window_breaks(h: Harness) -> None:
    """창이 끊기거나 표본이 적은 경우."""
    print("== window-breaks: 창 끊김 · 전달 실패 · 표본 부족 · 생성기 멈춤", flush=True)
    s = h.stack

    edge = s.edge("w10", target_percent=10)
    low = h.loadgen("low-sample", edge, duration=25, write=2, read=3, path="local-mock-edge")
    totals = [window_total(w) for w in low.windows]
    h.check("low-sample", f"표본을 부풀리지 않음: 창 전체 < {PROFILE['min_requests']}", all(0 < t < PROFILE["min_requests"] for t in totals), totals)
    verdicts = judged(low.windows, 10)
    h.check("low-sample", "기준 구현 judge가 INSUFFICIENT_SAMPLES로 UNKNOWN", all(v == "UNKNOWN" and "INSUFFICIENT_SAMPLES" in why for v, why in verdicts), verdicts[0])

    source = low.out / "windows.jsonl"
    lines = source.read_text().splitlines()
    cut = h.out / "cut-windows.jsonl"
    cut.write_text("\n".join([lines[0]]) + "\n")
    # 두 번째 창을 지우려면 창이 셋 필요하다. 끝에 하나를 더 붙여 가운데가 비게 만든다.
    third = json.loads(lines[1])
    third["window"] = {
        "start": _shift(third["window"]["start"], 10),
        "end": _shift(third["window"]["end"], 10),
    }
    cut.write_text(lines[0] + "\n" + json.dumps(third) + "\n")
    code, line = h.check_windows_command(cut)
    h.check("window-gap", "가운데 창을 지운 파일을 끊김으로 잡음, 종료 1", code == 1 and "연속성 문제 1건" in line, line)

    down = h.loadgen(
        "sink-down", s.source_url, duration=12, write=10, read=10,
        extra=("--window-url", f"http://127.0.0.1:{free_port()}/windows"),
    )  # fmt: skip
    rep = down.report
    h.check("sink-down", "플랫폼이 죽어도 부하는 끝까지 간다", rep["requests"]["write"]["attempts"] >= 120, rep["requests"]["write"]["attempts"])
    h.check("sink-down", "전달 실패를 세고 경고", rep["windows"]["delivery_failed"] == rep["windows"]["emitted"] > 0 and "WINDOW_DELIVERY_FAILED" in rep["warnings"], rep["windows"]["delivery_failed"])

    def stall(process: subprocess.Popen):
        time.sleep(8)  # 시작에 1초 안쪽이 걸린다. 10~20초 창 전체를 덮도록 멈춘다.
        process.send_signal(signal.SIGSTOP)
        time.sleep(14)
        process.send_signal(signal.SIGCONT)

    stalled = h.loadgen("generator-stall", edge, duration=30, write=20, read=20, path="local-mock-edge", during=stall)
    rep = stalled.report
    totals = [window_total(w) for w in stalled.windows]
    h.check("generator-stall", "생성기가 14초 멈춘 구간이 빈 창으로 드러남 (건너뛰지 않음)", 0 in totals, totals)
    h.check("generator-stall", "창은 끊기지 않고 이어짐", rep["windows"]["continuity_problems"] == [], len(stalled.windows))
    empty = totals.index(0)
    verdict = judged([stalled.windows[empty]], 10)[0]
    h.check("generator-stall", "빈 창을 기준 구현 judge가 UNKNOWN으로 읽음 (0을 정상으로 읽지 않음)", verdict[0] == "UNKNOWN" and "INSUFFICIENT_SAMPLES" in verdict[1], verdict)
    h.check("generator-stall", "생성기 지연 경고", "GENERATOR_LAGGING" in rep["warnings"], rep["load"]["scheduler_lag_ms"])


def scenario_soak(h: Harness) -> None:
    """3분 동안 돌려 메모리가 계속 늘어나는지 본다. 실행 위치를 정할 때 쓰는 값이다."""
    print("== soak: 50 + 150 req/s, 3분, Edge 대역", flush=True)
    edge = h.stack.edge("w10", target_percent=10)
    r = h.loadgen("soak", edge, duration=180, path="local-mock-edge", lock=True)
    h.check("soak", "대조 판정 PASS", r.code == 0, r.report["verdict"])
    h.check("soak", "창 18개가 끊기지 않고 나옴", r.report["windows"]["emitted"] == 18 and not r.report["windows"]["continuity_problems"], r.report["windows"]["emitted"])
    rss = h.resources["soak"]["rss_mib"]
    h.check("soak", "메모리가 처음의 1.5배 안에 머묾", rss["last_10_samples_avg"] <= rss["first_10_samples_avg"] * 1.5, rss)


def scenario_lock_rtt(h: Harness) -> None:
    """목적지 앱 → 출발지 DB 경로에 왕복 지연을 넣어 쓰기 잠금이 초당 50건을 견디는지 본다. 모의 지연이다."""
    print("== lock-rtt: 앱-DB 왕복 지연과 쓰기 잠금 (모의 지연)", flush=True)
    # 0ms는 대조군이다. 중계기 자체가 병목이 아닌지 본다.
    for rtt_ms in (0, 5, 20, 40):
        port = h.stack.delayed_target_app(rtt_ms)
        for percent in (10, 50):
            name = f"rtt{rtt_ms}-w{percent}"
            edge = h.stack.edge(name, target_percent=percent, target_port=port)
            r = h.loadgen(name, edge, duration=30, path="local-mock-edge", lock=True)
            rep, rec = r.report, r.report["reconciliation"]
            # 이 시나리오는 합격선을 정하지 않는다. 측정값을 남기고, 측정이 됐는지만 본다.
            h.check(
                "lock-rtt",
                f"왕복 {rtt_ms}ms · 목적지 {percent}%: 측정 완료, 유실 · 중복 0",
                rep["lock_probe"]["load"]["samples"] > 0
                and rec["nfr2"]["lost"] == 0
                and rec["nfr2"]["duplicated"] == 0,
                {
                    "nfr2": rec["nfr2"],
                    "retried": rec["counts"]["retried"],
                    "write_5xx": rep["requests"]["write"]["http_5xx"],
                    "write_p95_ms": rep["requests"]["write"]["p95_ms"],
                    "write_gap_ms": rep["write_gap"]["max_ms"],
                    "lock_wait_ms": rep["lock_probe"]["load"]["wait_ms"],
                    "lock_waiters_max": rep["lock_probe"]["load"]["waiting_sessions_max"],
                    "lock_timed_out": rep["lock_probe"]["load"]["timed_out"],
                    "judge_at_this_weight": [v for v, _ in judged(r.windows, percent)],
                },
            )


class _WindowReceiver:
    """창 수신 대역. 실제 수신부(T10)와 같은 순서로 본다: 토큰 → 문서. 실제 수신부 확인은 증거 폴더에 있다."""

    def __init__(self, token: str):
        receiver = self
        self.token = token
        self.windows: list[dict] = []
        self.probes = 0

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status: int, detail: str) -> None:
                payload = json.dumps({"detail": detail}).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if self.headers.get("Authorization") != f"Bearer {receiver.token}":
                    return self._send(401, "unauthorized")
                document = json.loads(body)
                try:
                    validate_window(document)
                except SchemaViolation:
                    receiver.probes += 1
                    return self._send(422, "invalid_window")
                receiver.windows.append(document)
                return self._send(201, "stored")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1/observability/windows"

    def __enter__(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc_info):
        self.httpd.shutdown()
        self.httpd.server_close()


def scenario_joint_rehearsal(h: Harness) -> None:
    """합동 측정 절차(docs/operations/t18-joint-measure.md)를 대역으로 처음부터 끝까지 돌린다."""
    print("== joint-rehearsal: 사전 점검 → 시험 실행 → 가중치 변경과 함께 본 측정 → 대조 → 맞추기", flush=True)
    s = h.stack
    name = "joint"
    edge = s.edge(name, target_percent=0)

    def cli_command(*arguments: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "bravo_loadgen.cli", *arguments],
            env=env or h._env(), capture_output=True, text=True,
        )  # fmt: skip

    def preflight(*extra: str, path: str = "local-mock-edge", env: dict | None = None) -> subprocess.CompletedProcess:
        return cli_command(
            "preflight", "--base-url", edge, "--plan-id", "plan-local", "--source-env", SOURCE_ENV,
            "--target-env", TARGET_ENV, "--duration", "60", "--measurement-path", path,
            "--out", str(h.out / "joint-preflight"), "--run-id", "t18-joint-preflight", *extra, env=env,
        )  # fmt: skip

    with _WindowReceiver(WINDOW_TOKEN) as receiver:
        shared = ("--generator", "laptop", "--window-url", receiver.url)
        rows_before = s.admin_execute("SELECT count(*) FROM messages")[0][0]

        result = preflight(*shared, env={**h._env(), "BRAVO_WINDOW_TOKEN": "wrong-token"})
        h.check("joint-rehearsal", "틀린 토큰이면 사전 점검 실패, 종료 2", result.returncode == 2 and "토큰을 거부했다" in result.stdout, result.returncode)
        result = cli_command(
            "preflight", "--base-url", edge, "--plan-id", "plan-local", "--source-env", "other",
            "--target-env", TARGET_ENV, "--duration", "60", "--measurement-path", "local-mock-edge",
            "--out", str(h.out / "joint-preflight"), "--run-id", "t18-joint-preflight",
        )  # fmt: skip
        h.check("joint-rehearsal", "환경 헤더가 계획과 다르면 사전 점검 실패, 종료 2", result.returncode == 2 and "등록된 값이 아니다" in result.stdout, result.returncode)
        result = preflight("--generator", "laptop", path="edge")
        h.check("joint-rehearsal", "대역을 edge로 적으면 CF-RAY가 없어 실패, 종료 2", result.returncode == 2 and "CF-RAY" in result.stdout, result.returncode)

        run_quiet("docker", "pause", s.pg_name)
        try:
            ready = httpx.get(f"{s.source_url}/readyz", trust_env=False, timeout=5).status_code
            result = preflight(*shared)
        finally:
            run_quiet("docker", "unpause", s.pg_name)
        h.check("joint-rehearsal", "DB가 멈추면 /readyz가 200이어도 사전 점검 실패", ready == 200 and result.returncode == 2 and "/api/health/db" in result.stdout, ready)

        h.check("joint-rehearsal", "실패한 사전 점검은 창도 행도 남기지 않음",
                not receiver.windows and s.admin_execute("SELECT count(*) FROM messages")[0][0] == rows_before,
                len(receiver.windows))  # fmt: skip
        result = preflight(*shared)
        h.check("joint-rehearsal", "정상이면 사전 점검 통과, 빈 문서로만 확인", result.returncode == 0 and not receiver.windows and receiver.probes > 0, receiver.probes)

        # 시험 실행: 표본이 적어 판정은 UNKNOWN이 정상이다. 경로만 본다.
        trial = h.loadgen(
            "joint-trial", edge, duration=21, write=5, read=15, path="local-mock-edge", subcommand="measure",
            extra=shared,
        )  # fmt: skip
        rep = trial.report
        h.check("joint-rehearsal", "시험 실행: 대조 PASS, 창 전달 실패 0", trial.code == 0 and rep["windows"]["delivery_failed"] == 0, rep["windows"]["delivery_failed"])
        verdicts = judged(trial.windows, 0)
        h.check("joint-rehearsal", "시험 실행의 창은 표본 부족으로 UNKNOWN", all(v == "UNKNOWN" and "INSUFFICIENT_SAMPLES" in why for v, why in verdicts), verdicts[0])

        changes: list[dict] = []

        def change_weights(_process):
            time.sleep(10)
            for weight in (10, 50, 100, 0):
                requested = datetime.now(UTC).isoformat()
                s.set_edge_weight(name, weight)
                changes.append({"weight_target": weight, "request_at": requested, "confirmed_at": datetime.now(UTC).isoformat()})
                time.sleep(30)

        main = h.loadgen(
            "joint-main", edge, duration=135, path="local-mock-edge", lock=True, subcommand="measure",
            extra=shared, during=change_weights,
        )  # fmt: skip
        rep, rec = main.report, main.report["reconciliation"]
        h.check("joint-rehearsal", "본 측정: 대조 PASS, 실패 · 유실 · 중복 0", main.code == 0 and rec["nfr2"] == {"failed": 0, "lost": 0, "duplicated": 0}, rec["nfr2"])
        h.check("joint-rehearsal", "본 측정: 창이 수신에 빠짐없이 도착", rep["windows"]["delivery_failed"] == 0 and len(receiver.windows) == len(trial.windows) + len(main.windows), len(receiver.windows))
        h.check("joint-rehearsal", "노트북 · 대역 값이라 판정 입력으로 표시하지 않음", rep["sli_usable_for_judgement"] is False and rep["measurement"]["generator"] == "laptop" and "NOT_EDGE_MEASURED" in rep["warnings"], rep["measurement"]["path"])
        h.check("joint-rehearsal", "리포트에 부하 전 왕복 기준값이 남음", rep["measurement"]["preflight"]["health_rtt_ms"]["samples"] == 5, rep["measurement"]["preflight"]["health_rtt_ms"])

        again = h.reconcile(main.out)
        h.check("joint-rehearsal", "끝난 뒤 reconcile도 PASS, 표시 유지", again.code == 0 and again.report["measurement"]["generator"] == "laptop", again.report["verdict"])
        code, line = h.check_windows_command(main.out / "windows.jsonl")
        h.check("joint-rehearsal", "창 파일이 스키마 · 연속성 통과", code == 0, line)

        receipts = h.out / "joint-receipts.jsonl"
        receipts.write_text("".join(
            json.dumps({"plan_id": "plan-local", "run_id": rep["run_id"], "status": "CONFIRMED", "reason": "SHIFT",
                        "revision": index + 1, "lb_ref": "local-stand-in", **change}) + "\n"
            for index, change in enumerate(changes)
        ))  # fmt: skip
        aligned = cli_command("align", "--out", str(main.out), "--receipts", str(receipts))
        result = json.loads((main.out / "weight-alignment.json").read_text())
        steps = result["steps"]
        print(aligned.stdout, flush=True)
        h.check("joint-rehearsal", "가중치 4단계가 모두 창에서 확인됨", aligned.returncode == 0 and [s_["weight_target"] for s_ in steps] == [10, 50, 100, 0] and all(s_["observation"] == "OBSERVED" for s_ in steps), [s_["reflect_upper_bound_s"] for s_ in steps])
        h.check("joint-rehearsal", "가중치가 있는 환경마다 p95가 남음", all(s_["target_p95_ms_max"] is not None for s_ in steps[:3]) and steps[3]["source_p95_ms_max"] is not None, [(s_["source_p95_ms_max"], s_["target_p95_ms_max"]) for s_ in steps])
        other = h.out / "joint-other-receipts.jsonl"
        other.write_text(json.dumps({"plan_id": "plan-local", "run_id": "t18-another-run", "status": "CONFIRMED", "weight_target": 10, "request_at": changes[0]["request_at"]}) + "\n")
        refused = cli_command("align", "--out", str(main.out), "--receipts", str(other))
        h.check("joint-rehearsal", "다른 실행의 영수증은 거부, 종료 2", refused.returncode == 2 and "실행 ID" in refused.stderr, refused.returncode)


def scenario_preflight_write_block(h: Harness) -> None:
    """실제 demo-chat의 /api/info로 쓰기 차단 사전 점검을 본다. 부하는 보내지 않는다."""
    print("== preflight-write-block: 목적지 쓰기 차단이 켜져 있으면 사전 점검이 막는지", flush=True)
    s = h.stack
    name = "write-block-preflight"
    edge = s.edge(name, target_percent=100)
    rows_before = s.admin_execute("SELECT count(*) FROM messages")[0][0]

    def preflight() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "bravo_loadgen.cli", "preflight", "--base-url", edge, "--plan-id", "plan-local",
             "--source-env", SOURCE_ENV, "--target-env", TARGET_ENV, "--duration", "60",
             "--measurement-path", "local-mock-edge", "--out", str(h.out / "write-block-preflight"),
             "--run-id", "t18-write-block-preflight"],
            env=h._env(), capture_output=True, text=True,
        )  # fmt: skip

    def line(result: subprocess.CompletedProcess) -> str:
        return next((row.strip() for row in result.stdout.splitlines() if "쓰기 차단" in row), "")

    result = preflight()
    h.check("preflight-write-block", "가중치 100%, 양쪽 열림: 통과", result.returncode == 0 and f"{TARGET_ENV} 열림 5" in result.stdout, line(result))
    s.set_write_block(True, (s.target_url,))
    try:
        result = preflight()
        h.check("preflight-write-block", "가중치 100%, 목적지 닫힘: 실패하고 여는 명령을 알림, 종료 2",
                result.returncode == 2 and f"{TARGET_ENV} 닫힘 5" in result.stdout and "write_switch.py open" in result.stdout,
                line(result))  # fmt: skip
        s.set_edge_weight(name, 0)
        result = preflight()
        h.check("preflight-write-block", "가중치 0%, 목적지 닫힘: 보이지 않아 확인 못 함, status를 알림",
                result.returncode == 0 and f"{TARGET_ENV} 응답 없어 확인 못 함" in result.stdout and "write_switch.py status" in result.stdout,
                line(result))  # fmt: skip
        s.set_write_block(True, (s.source_url,))
        result = preflight()
        h.check("preflight-write-block", "가중치 0%, 출발지 닫힘: 실패, 목적지 명령은 알리지 않음, 종료 2",
                result.returncode == 2 and f"{SOURCE_ENV} 닫힘 5" in result.stdout and "write_switch.py open" not in result.stdout,
                line(result))  # fmt: skip
    finally:
        s.set_write_block(False)
    rows_after = s.admin_execute("SELECT count(*) FROM messages")[0][0]
    h.check("preflight-write-block", "사전 점검은 행을 남기지 않음", rows_after == rows_before, rows_after - rows_before)


def _shift(moment: str, seconds: int) -> str:
    shifted = datetime.fromisoformat(moment) + timedelta(seconds=seconds)
    return shifted.isoformat().replace("+00:00", "Z")


SCENARIOS = {
    "direct": scenario_direct,
    "mock-edge": scenario_mock_edge,
    "write-block": scenario_write_block,
    "ack-loss": scenario_ack_loss,
    "failed-but-stored": scenario_failed_but_stored,
    "tampering": scenario_tampering,
    "all-failed": scenario_all_failed,
    "edge-error": scenario_edge_error,
    "db-pause": scenario_db_pause,
    "window-breaks": scenario_window_breaks,
    "soak": scenario_soak,
    "lock-rtt": scenario_lock_rtt,
    "joint-rehearsal": scenario_joint_rehearsal,
    "preflight-write-block": scenario_preflight_write_block,
}


def environment(demo_chat: Path) -> dict:
    def digest(image: str) -> str:
        return run_quiet("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image).stdout.strip()

    return {
        "kind": "local-measurement",
        "edge": "not measured through Cloudflare; mock-edge scenarios use a local nginx stand-in",
        "platform_commit": run_quiet("git", "rev-parse", "HEAD", cwd=REPO).stdout.strip(),
        "platform_dirty": bool(run_quiet("git", "status", "--porcelain", cwd=REPO).stdout.strip()),
        "demo_chat_commit": run_quiet("git", "rev-parse", "HEAD", cwd=demo_chat).stdout.strip(),
        "postgres_image": json.loads(digest(POSTGRES_IMAGE)),
        "edge_image": json.loads(digest(EDGE_IMAGE)),
        "moto_version": MOTO_VERSION,
        "demo_chat_runtime": "native uvicorn single process per environment (not the container image)",
        "python": sys.version.split()[0],
        "machine": {
            "arch": run_quiet("uname", "-m").stdout.strip(),
            "cpu": run_quiet("sysctl", "-n", "machdep.cpu.brand_string").stdout.strip(),
            "logical_cores": psutil.cpu_count(),
            "memory_gib": round(psutil.virtual_memory().total / 2**30),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo-chat", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=60, help="주 측정(direct, mock-edge)의 부하 시간")
    parser.add_argument("--only", nargs="*", choices=sorted(SCENARIOS), help="이 시나리오만 돌린다")
    parser.add_argument("--container-prefix", default="bravo-t18", help="임시 컨테이너 이름의 앞부분")
    args = parser.parse_args()
    if shutil.which("docker") is None:
        raise SystemExit("docker가 필요하다")
    demo_chat = args.demo_chat.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    with Stack(demo_chat, out, container_prefix=args.container_prefix) as stack:
        harness = Harness(stack, out, args.duration)
        for name in args.only or SCENARIOS:
            SCENARIOS[name](harness)

    failed = [c for c in harness.checks if not c["ok"]]
    (out / "results.json").write_text(
        json.dumps(
            {
                "environment": environment(demo_chat),
                "checks_total": len(harness.checks),
                "checks_failed": len(failed),
                "load_average_1_5_15m_at_start": harness.load_average,
                "generator_resources": harness.resources,
                "checks": harness.checks,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(f"\n검사 {len(harness.checks)}건, 실패 {len(failed)}건")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
