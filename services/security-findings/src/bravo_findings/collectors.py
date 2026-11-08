"""끌어오는 수집기 (AWS Security Hub, Google Cloud SCC). 읽기 전용이고, 커서는 끝까지 성공했을 때만 전진한다."""

import logging
from collections import Counter
from collections.abc import Callable, Iterator
from typing import Protocol

from bravo_findings.gcp_scc import SccSource as SccSource
from bravo_findings.ingest import Ingestor, collection_error_code
from bravo_findings.securityhub import SecurityHubSource as SecurityHubSource

log = logging.getLogger("bravo_findings")

# 최초 조회는 전체 보존 범위. 증분은 지연 도착을 위한 5분 중첩.
OVERLAP_MS = 5 * 60_000


class PullSource(Protocol):
    source: str

    def fetch(self, since_ms: int, until_ms: int) -> Iterator[dict]: ...


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
