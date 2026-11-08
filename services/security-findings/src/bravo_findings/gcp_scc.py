"""Security Command Center v2 ListFindings for one project/folder/organization. Read-only."""

import re
from collections.abc import Iterator
from typing import Any

from bravo_findings.normalize import CollectionError, NormalizationError, SourceNotEnabled, epoch_ms

# Numeric scopes only: finding names always carry the number, so the scope check below can hold.
PARENT_RE = re.compile(r"(?:projects/[0-9]{6,20}|folders/[0-9]{1,20}|organizations/[0-9]{1,20})")


SERVICE_RE = re.compile(r"[a-z]+(?:-[a-z]+){1,4}")


class SccSource:
    """`session` is an authenticated HTTP session (google.auth AuthorizedSession). Needs
    `securitycenter.findings.list` and `securitycentermanagement.securityCenterServices.get` on
    `parent`. v2 is the only API for SCC activated after 2024-12-09; without data residency the
    location is `global`.

    SCC answers an empty `{}` both when nothing was found and when no detector runs, so a page of
    zero findings is only trusted after each detection service in `services` reads ENABLED. A read
    that says DISABLED is NOT_ENABLED (like an absent GuardDuty detector); anything else is UNKNOWN."""

    source = "gcp_scc"
    endpoint = "https://securitycenter.googleapis.com/v2"
    management = "https://securitycentermanagement.googleapis.com/v1"

    def __init__(
        self,
        session: Any,
        parent: str,
        page_size: int = 100,
        services: tuple[str, ...] = ("security-health-analytics",),
    ):
        if (
            not PARENT_RE.fullmatch(parent)
            or not 1 <= page_size <= 1000
            or type(services) is not tuple
            or not services
            or not all(isinstance(s, str) and SERVICE_RE.fullmatch(s) for s in services)
        ):
            raise ValueError("invalid_scc_scope")
        self.session, self.parent, self.page_size = session, parent, page_size
        self.services = services
        self.name_re = re.compile(re.escape(parent) + r"/sources/[^/]+(?:/locations/global)?/findings/[^/]+")

    def _require_enabled(self) -> None:
        for service in self.services:
            url = f"{self.management}/{self.parent}/locations/global/securityCenterServices/{service}"
            response = self.session.get(url, params={}, timeout=30)
            response.raise_for_status()
            doc = response.json()
            state = doc.get("effectiveEnablementState") if isinstance(doc, dict) else None
            if state == "DISABLED":
                raise SourceNotEnabled("scc_service_disabled")
            if state != "ENABLED":
                raise CollectionError("scc_service_state_unknown")

    def _pages(self, params: dict) -> Iterator[list]:
        url = f"{self.endpoint}/{self.parent}/sources/-/locations/global/findings"
        seen = set()
        for _ in range(200):
            response = self.session.get(url, params=params, timeout=30)
            response.raise_for_status()
            page = response.json()
            # Empty repeated fields are omitted from the JSON response.
            results = page.get("listFindingsResults", []) if isinstance(page, dict) else None
            if not isinstance(results, list):
                raise CollectionError("incomplete_provider_response")
            yield results
            token = page.get("nextPageToken")
            if token is None or token == "":
                return
            if not isinstance(token, str) or token in seen:
                raise CollectionError("invalid_pagination")
            seen.add(token)
            params["pageToken"] = token
        raise CollectionError("pagination_limit")

    def fetch(self, since_ms: int, until_ms: int) -> Iterator[dict]:
        if any(type(v) is not int for v in (since_ms, until_ms)) or not 0 <= since_ms <= until_ms:
            raise ValueError("invalid_collection_window")
        self._require_enabled()
        params: dict[str, Any] = {
            "filter": f"eventTime >= {since_ms} AND eventTime <= {until_ms}",
            "pageSize": self.page_size,
        }
        seen = set()
        for results in self._pages(params):
            for item in results:
                finding = item.get("finding") if isinstance(item, dict) else None
                if not isinstance(finding, dict):
                    raise CollectionError("incomplete_findings")
                name = finding.get("name")
                if not isinstance(name, str) or not self.name_re.fullmatch(name):
                    raise CollectionError("finding_scope_mismatch")
                if name in seen:
                    raise CollectionError("duplicate_findings")
                seen.add(name)
                try:
                    event_ms = epoch_ms(finding["eventTime"])
                except (KeyError, TypeError, ValueError, AttributeError, NormalizationError):
                    raise CollectionError("incomplete_findings") from None
                # Resolving or re-observing a finding moves eventTime; retry the unchanged cursor
                # instead of certifying a window the provider no longer matches.
                if not since_ms <= event_ms <= until_ms:
                    raise CollectionError("finding_window_changed")
                yield finding
