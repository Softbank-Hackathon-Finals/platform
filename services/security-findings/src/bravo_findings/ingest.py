import logging
from collections import Counter
from collections.abc import Iterable

from bravo_findings.mask import mask
from bravo_findings.normalize import CollectionError, Dropped, NormalizationError, Normalizer
from bravo_findings.store import Store

log = logging.getLogger("bravo_findings")


class Ingestor:
    """원본 묶음을 정규화해 저장한다. 끌어오는 수집기와 밀어 넣는 API가 같이 쓴다."""

    def __init__(self, store: Store, normalizer: Normalizer):
        self.store, self.normalizer = store, normalizer

    def ingest(self, source: str, environment_id: str, raws: Iterable[dict], now: int) -> Counter:
        """결과 건수를 돌려준다. 출처가 오류를 보고하면 CollectionError를 그대로 올린다(호출한 쪽이 FAILED로 기록)."""
        counts: Counter = Counter()
        for raw in raws:
            try:
                finding = self.normalizer.normalize(
                    source, raw, environment_id=environment_id, collected_time=now
                )
            except Dropped:
                counts["dropped"] += 1
                continue
            except NormalizationError as e:
                # 한 건이 이상하다고 묶음 전체를 버리지 않는다. 격리하고 계속한다.
                self.store.quarantine(source, environment_id, now, str(e))
                log.warning("quarantined %s/%s: %s", source, environment_id, e)
                counts["quarantined"] += 1
                continue
            masked = mask({k: v for k, v in raw.items() if not k.startswith("_")})
            counts[self.store.upsert(finding, masked)] += 1
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
