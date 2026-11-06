"""끌어오는 수집기 (AWS Security Hub, Google Cloud SCC). 읽기 전용이고, 커서는 끝까지 성공했을 때만 전진한다."""

import logging
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Any, Protocol

from bravo_findings.ingest import Ingestor, collection_error_code
from bravo_findings.securityhub import SecurityHubSource as SecurityHubSource

log = logging.getLogger("bravo_findings")

# 최초 조회는 전체 보존 범위. 증분은 지연 도착을 위한 5분 중첩.
OVERLAP_MS = 5 * 60_000


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class PullSource(Protocol):
    source: str

    def fetch(self, since_ms: int, until_ms: int) -> Iterator[dict]: ...


class SccSource:
    """Security Command Center v1 REST ListFindings. session은 인증된 HTTP 세션
    (예: google.auth.transport.requests.AuthorizedSession). 필요한 권한: SCC 결과 조회 전용 역할.
    필터 문법(event_time 비교)은 실제 프로젝트에서 확인한다."""

    source = "gcp_scc"
    endpoint = "https://securitycenter.googleapis.com/v1"

    def __init__(self, session: Any, parent: str, page_size: int = 100):
        self.session, self.parent, self.page_size = session, parent, page_size

    def fetch(self, since_ms: int, until_ms: int) -> Iterator[dict]:
        params: dict[str, Any] = {
            "filter": f'event_time >= "{iso(since_ms)}" AND event_time <= "{iso(until_ms)}"',
            "pageSize": self.page_size,
        }
        url = f"{self.endpoint}/{self.parent}/sources/-/findings"
        while True:
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            page = response.json()
            for item in page.get("listFindingsResults", []):
                if "finding" in item:
                    yield item["finding"]
            if not page.get("nextPageToken"):
                return
            params["pageToken"] = page["nextPageToken"]


def run_once(
    ingestor: Ingestor, pull: PullSource, environment_id: str, clock: Callable[[], int]
) -> Counter:
    """한 번 수집한다. 실패하면 커서를 그대로 두고 FAILED로 기록한다(다음 수집이 같은 범위를 다시 가져온다)."""
    store = ingestor.store
    row = store.source(pull.source, environment_id)
    if row is None:
        store.register_source(pull.source, environment_id)
        row = store.source(pull.source, environment_id)
    if not row.enabled:
        return Counter({"disabled": 1})
    now = clock()
    since = max(0, int(row.cursor) - OVERLAP_MS) if row.cursor else 0
    try:
        raws = []
        for raw in pull.fetch(since, now):
            raws.append(raw)
            if len(raws) > 10_000:
                raise ValueError("collection batch limit")
        with store.batch():
            counts = ingestor.ingest(pull.source, environment_id, raws, now)
            if counts["source_errors"]:
                store.record_failure(pull.source, environment_id, now, "SOURCE_UNHEALTHY")
            else:
                store.record_success(pull.source, environment_id, now, cursor=str(now))
            if store.source(pull.source, environment_id).error_code:
                counts["failed"] += 1
            return counts
    except Exception as e:  # noqa: BLE001 — errors are stored as codes, never payloads
        code = collection_error_code(e)
        store.record_failure(pull.source, environment_id, now, code)
        log.warning("collection failed: %s", code)
        return Counter({"failed": 1})
