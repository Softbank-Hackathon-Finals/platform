"""모의 환경 시험 도우미: 설정 읽기, 컷오버 전 상태로 되돌리기, 쓰기 DB 감시."""

import json
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import pytest

from bravo_cutover.remote.db import DbControl, WriteRefusal, try_app_write
from bravo_cutover.remote.kube import Kube, WriteBlock
from bravo_cutover.remote.pgbouncer import PgBouncerAdmin, PgBouncerConfig, retarget_ini
from bravo_cutover.sites import Config, Site, load_config

SITES_FILE = Path(__file__).resolve().parents[1] / "testing" / ".state" / "sites.json"
REPLICATED = ["public.attachments", "public.messages", "cutover.barrier", "cutover.probe"]
needs_mockenv = pytest.mark.skipif(not SITES_FILE.exists(), reason="모의 환경 없음 (testing/mockenv.py up)")


def _pair() -> tuple[str, str]:
    """모의 환경의 (출발지, 목적지) 환경 ID. 출발지는 설정의 origin이다. 이름은 mockenv.py up --source/--target이
    정하므로 시험은 고정하지 않는다(ADR-0034, A9). 모의 환경이 없으면 기본 이름을 돌려준다(시험은 건너뛴다)."""
    if not SITES_FILE.exists():
        return "env-aws", "env-onprem"
    config = load_config(SITES_FILE)
    [target] = [env for env in config.sites if env != config.origin]
    return config.origin, target


SRC_ENV, DST_ENV = _pair()
REFS = {SRC_ENV: "mock/src", DST_ENV: "mock/dst"}


def mock_config(**settings) -> Config:
    config = load_config(SITES_FILE)
    return config.model_copy(update={"settings": config.settings.model_copy(update=settings)})


def reset(src: Site, dst: Site) -> None:
    """컷오버 전 상태: 출발지만 쓰기, 정방향 구독만 켜짐, 두 PgBouncer는 출발지 · RESUME, 진입점 차단 해제."""
    kube = Kube(src.kube_context)
    sdb, ddb = DbControl(src.db_control_dsn), DbControl(dst.db_control_dsn)
    if ddb.can_login(dst.app_role):
        ddb.set_login(dst.app_role, False)
    ddb.terminate_sessions(dst.app_role)
    if sdb.subscription(src.subscription).enabled:
        # 목적지에 쓴 것이 출발지에 다 따라온 뒤 역방향을 끈다. 목적지가 쉬면 위치(LSN) 보고가 늦게 갱신되므로
        # 행 수로 본다(목적지는 이미 닫혀 새 쓰기가 없다).
        deadline = time.monotonic() + 15
        while any(sdb.row_count(t) != ddb.row_count(t) for t in REPLICATED):
            assert time.monotonic() < deadline, "reverse replication did not catch up"
            time.sleep(0.1)
        sdb.set_subscription(src.subscription, False, src.subscription_owner)
    if not ddb.subscription(dst.subscription).enabled:
        ddb.set_subscription(dst.subscription, True, dst.subscription_owner)
    sdb.raise_sequence("public.messages_id_seq", sdb.max_value("public.messages", "id") + 1)
    if not sdb.can_login(src.app_role):
        sdb.set_login(src.app_role, True)
    for site in (src, dst):
        pool = PgBouncerAdmin(site.pgbouncer_admin_dsn, site.pgbouncer_database)
        PgBouncerConfig(kube, site.namespace, site.pgbouncer_selector, site.pgbouncer_ini).retarget(
            site.pgbouncer_database, src.db_host, src.db_port, src.db_name)
        pool.reload()
        pool.resume()
        ini = kube.read_configmap(site.namespace, site.pgbouncer_configmap, site.pgbouncer_configmap_key)
        kube.patch_configmap(site.namespace, site.pgbouncer_configmap, site.pgbouncer_configmap_key,
                             retarget_ini(ini, site.pgbouncer_database, src.db_host, src.db_port, src.db_name))
        WriteBlock(kube, site.namespace, site.app_selector).set(False)
    # 두 PgBouncer가 기억한 로그인 실패(server_login_retry)가 풀릴 때까지 기다린다
    deadline = time.monotonic() + 40
    for site in (src, dst):
        while try_app_write(site.pgbouncer_app_dsn, "reset") != WriteRefusal.ACCEPTED:
            assert time.monotonic() < deadline, f"{site.environment_id} path does not accept writes after reset"
            time.sleep(0.5)


class WriterWatch:
    """실행 중 각 DB가 앱 역할의 쓰기를 받을 수 있는지 계속 읽는다 (spec 완성 기준 1).

    "쓰기 가능"은 앱 역할이 로그인할 수 있거나, 이미 붙어 있는 앱 역할 세션이 남아 있는 것이다
    (NOLOGIN 전에 연결된 세션은 계속 쓸 수 있으므로). sample은 (시각, 출발지, 목적지)이고 None은 읽지 못한 것이다.
    읽지 못한 표본이 하나라도 있으면 안전을 확인한 것이 아니므로 assert_safe()가 실패한다.
    """

    def __init__(self, src: Site, dst: Site, interval_s: float = 0.02):
        self._dbs = [(DbControl(s.db_control_dsn), s.app_role) for s in (src, dst)]
        self._interval, self._stop = interval_s, threading.Event()
        self.samples: list[tuple[float, bool | None, bool | None]] = []
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _read(self, db, role):
        try:
            return db.can_login(role) or db.sessions(role) > 0
        except Exception:  # noqa: BLE001
            return None

    def _loop(self):
        while not self._stop.is_set():
            self.samples.append((time.monotonic(), *(self._read(db, role) for db, role in self._dbs)))
            time.sleep(self._interval)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(2)

    def both_writable(self):
        return [s for s in self.samples if s[1] and s[2]]

    def unreadable(self):
        return [s for s in self.samples if s[1] is None or s[2] is None]

    def assert_safe(self):
        assert len(self.samples) >= 2, "too few samples to judge"
        assert self.unreadable() == [], f"{len(self.unreadable())} samples could not be read"
        assert self.both_writable() == [], f"both writable at {self.both_writable()[:3]}"


def post_message(url: str, key: uuid.UUID, body: str, author: str = "test", retry_s: float = 20) -> int:
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
