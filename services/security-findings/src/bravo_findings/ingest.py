import logging
from collections import Counter
from collections.abc import Iterable

from bravo_findings.mask import source_projection
from bravo_findings.normalize import CollectionError, Dropped, NormalizationError, Normalizer
from bravo_findings.store import EventConflict, Store

log = logging.getLogger("bravo_findings")


class Ingestor:
    """원본 묶음을 정규화해 저장한다. 끌어오는 수집기와 밀어 넣는 API가 같이 쓴다."""

    def __init__(self, store: Store, normalizer: Normalizer):
        self.store, self.normalizer = store, normalizer

    def ingest(self, source: str, environment_id: str, raws: Iterable[dict], now: int) -> Counter:
        """결과·격리·출처 오류 건수를 돌려준다. 호출자는 오류가 있으면 성공/커서 전진을 금지한다."""
        counts: Counter = Counter()
        with self.store.batch():
            for raw in raws:
                try:
                    finding = self.normalizer.normalize(
                        source, raw, environment_id=environment_id, collected_time=now
                    )
                    if not 0 < finding["time"] <= now + 300_000:
                        raise NormalizationError("INVALID_EVENT_TIME")
                    counts[self.store.upsert(finding, source_projection(source, raw))] += 1
                except Dropped:
                    counts["dropped"] += 1
                except CollectionError:
                    counts["source_errors"] += 1
                except (
                    NormalizationError,
                    EventConflict,
                    ValueError,
                    TypeError,
                    KeyError,
                    AttributeError,
                    OverflowError,
                ):
                    # Never log exception text: schema/type errors can contain the raw secret.
                    self.store.quarantine(source, environment_id, now, "NORMALIZATION_FAILED")
                    log.warning("event quarantined: NORMALIZATION_FAILED")
                    counts["quarantined"] += 1
        return counts


def collection_error_code(exc: BaseException) -> str:
    if isinstance(exc, CollectionError):
        return "SOURCE_UNHEALTHY"
    name = type(exc).__name__
    text = str(exc)
    if "AccessDenied" in text or "403" in text or name in {"PermissionError"}:
        return "ACCESS_DENIED"
    if "Throttl" in text or "429" in text or "RateLimit" in text:
        return "RATE_LIMITED"
    if "Timeout" in name or "timed out" in text:
        return "TIMEOUT"
    return "API_ERROR"
