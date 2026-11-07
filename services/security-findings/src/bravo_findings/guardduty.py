"""Read one account/region detector; never enable a paid AWS service."""

import re
from collections.abc import Iterator

from bravo_findings.normalize import CollectionError, epoch_ms


class SourceNotEnabled(CollectionError):
    """A successful provider read established that detection is not enabled."""


class GuardDutySource:
    source = "aws_guardduty"

    def __init__(self, client, *, account_id: str, region: str, detector_id: str | None = None):
        if not re.fullmatch(r"[0-9]{12}", account_id) or client.meta.region_name != region:
            raise ValueError("invalid_guardduty_scope")
        if detector_id is not None and not re.fullmatch(r"[0-9a-f]{32}", detector_id):
            raise ValueError("invalid_detector_id")
        self.client, self.account_id, self.region = client, account_id, region
        self.detector_id = detector_id

    @staticmethod
    def _pages(call, key, **kwargs):
        seen = set()
        for _ in range(200):
            page = call(**kwargs)
            if not isinstance(page, dict) or not isinstance(page.get(key), list):
                raise CollectionError("incomplete_provider_response")
            yield page[key]
            token = page.get("NextToken")
            # The live ListFindings ends with NextToken "" (11-08 sample findings); ListDetectors omits it.
            if token is None or token == "":
                return
            if not isinstance(token, str) or token in seen:
                raise CollectionError("invalid_pagination")
            seen.add(token)
            kwargs["NextToken"] = token
        raise CollectionError("pagination_limit")

    def _detector(self):
        if self.detector_id is None:
            ids = [
                item
                for page in self._pages(self.client.list_detectors, "DetectorIds", MaxResults=50)
                for item in page
            ]
            if not ids:
                raise SourceNotEnabled("detector_absent")
            if (
                len(ids) != 1
                or not isinstance(ids[0], str)
                or not re.fullmatch(r"[0-9a-f]{32}", ids[0])
            ):
                raise CollectionError("ambiguous_detector")
            self.detector_id = ids[0]
        state = self.client.get_detector(DetectorId=self.detector_id).get("Status")
        if state == "DISABLED":
            raise SourceNotEnabled("detector_disabled")
        if state != "ENABLED":
            raise CollectionError("detector_state_unknown")
        return self.detector_id

    def fetch(self, since_ms: int, until_ms: int) -> Iterator[dict]:
        if any(type(v) is not int for v in (since_ms, until_ms)) or not 0 <= since_ms <= until_ms:
            raise ValueError("invalid_collection_window")
        detector = self._detector()
        seen = set()
        pages = self._pages(
            self.client.list_findings,
            "FindingIds",
            DetectorId=detector,
            FindingCriteria={"Criterion": {"updatedAt": {"Gte": since_ms, "Lte": until_ms}}},
            MaxResults=50,
        )
        for ids in pages:
            if len(ids) > 50 or any(
                not isinstance(i, str) or not re.fullmatch(r"[0-9a-f]{32}", i) for i in ids
            ):
                raise CollectionError("invalid_finding_ids")
            if len(set(ids)) != len(ids) or seen.intersection(ids):
                raise CollectionError("duplicate_finding_ids")
            seen.update(ids)
            if not ids:
                continue
            result = self.client.get_findings(DetectorId=detector, FindingIds=ids)
            findings = result.get("Findings")
            if not isinstance(findings, list) or len(findings) != len(ids):
                raise CollectionError("incomplete_findings")
            returned = [f.get("Id") if isinstance(f, dict) else None for f in findings]
            if any(not isinstance(i, str) for i in returned) or set(returned) != set(ids):
                raise CollectionError("finding_identity_mismatch")
            for finding in findings:
                if (
                    finding.get("AccountId") != self.account_id
                    or finding.get("Region") != self.region
                    or finding.get("Service", {}).get("DetectorId") != detector
                ):
                    raise CollectionError("finding_scope_mismatch")
                # The finding can change between List and Get. Retry the unchanged
                # cursor instead of certifying an inconsistent collection window.
                if not since_ms <= epoch_ms(finding.get("UpdatedAt")) <= until_ms:
                    raise CollectionError("finding_window_changed")
                yield finding
