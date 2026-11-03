"""모의 환경 시험 도우미: 설정 읽기, 컷오버 전 상태로 되돌리기, 쓰기 DB 감시."""

import threading
import time
from pathlib import Path

import pytest

from bravo_cutover.remote.db import DbControl
from bravo_cutover.remote.kube import Kube, WriteBlock
from bravo_cutover.remote.pgbouncer import PgBouncerAdmin, PgBouncerConfig, retarget_ini
from bravo_cutover.sites import Config, Site, load_config

SITES_FILE = Path(__file__).resolve().parents[1] / "testing" / ".state" / "sites.json"
needs_mockenv = pytest.mark.skipif(not SITES_FILE.exists(), reason="모의 환경 없음 (testing/mockenv.py up)")


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
        # 목적지에 쓴 것이 출발지에 다 따라온 뒤 역방향을 끈다
        wanted, deadline = int(ddb.current_wal_lsn()), time.monotonic() + 15
        while int(sdb.received_lsn(src.subscription) or 0) < wanted:
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


class WriterWatch:
    """실행 중 앱 역할이 로그인할 수 있는 DB 수를 계속 읽는다 (spec 완성 기준 1).

    sample은 (시각, 출발지, 목적지) 튜플. None은 그 순간 읽지 못했다는 뜻이다.
    """

    def __init__(self, src: Site, dst: Site, interval_s: float = 0.02):
        self._dbs = [(DbControl(s.db_control_dsn), s.app_role) for s in (src, dst)]
        self._interval, self._stop = interval_s, threading.Event()
        self.samples: list[tuple[float, bool | None, bool | None]] = []
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def _read(self, db, role):
        try:
            return db.can_login(role)
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
