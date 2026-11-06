"""Bounded, account/region-bound Security Hub reads; never enable the service."""

import re
import time
from collections.abc import Iterator

from botocore.exceptions import ClientError

from bravo_findings.normalize import CollectionError, epoch_ms


def _iso(ms: int) -> str:
    from datetime import UTC, datetime

    return (
        datetime.fromtimestamp(ms / 1000, UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class SecurityHubSource:
    source = "aws_security_hub"

    def __init__(self, client, *, account_id: str, region: str, page_size: int = 100):
        if (
            not isinstance(account_id, str)
            or re.fullmatch(r"[0-9]{12}", account_id) is None
            or not isinstance(region, str)
            or re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region) is None
            or client.meta.region_name != region
            or type(page_size) is not int
            or not 1 <= page_size <= 100
        ):
            raise ValueError("invalid_securityhub_scope")
        # First qualified path is commercial AWS. Other partitions need qualification.
        if region.startswith(("cn-", "us-gov-", "us-iso-", "us-isob-")):
            raise ValueError("unsupported_partition")
        self.client, self.account_id, self.region = client, account_id, region
        self.page_size = page_size

    @staticmethod
    def _call(call, **kwargs):
        try:
            return call(**kwargs)
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") == "InvalidAccessException":
                # AWS defines 401 as lack of permission, not proof of absent detection.
                raise PermissionError("securityhub_access_unavailable") from None
            raise

    def _hub(self):
        hub = self._call(self.client.describe_hub)
        expected = ":".join(
            ("arn", "aws", "securityhub", self.region, self.account_id, "hub/default")
        )
        if not isinstance(hub, dict) or hub.get("HubArn") != expected:
            raise CollectionError("hub_scope_mismatch")
        subscribed = hub.get("SubscribedAt")
        if not isinstance(subscribed, str) or not 0 < epoch_ms(subscribed):
            raise CollectionError("hub_subscription_unknown")
        return expected, subscribed

    def fetch(self, since_ms: int, until_ms: int) -> Iterator[dict]:
        if any(type(v) is not int for v in (since_ms, until_ms)) or not 0 <= since_ms <= until_ms:
            raise ValueError("invalid_collection_window")
        deadline = time.monotonic() + 120

        def check_deadline():
            if time.monotonic() >= deadline:
                raise TimeoutError("securityhub_collection_deadline")

        before = self._hub()
        check_deadline()
        kwargs = {
            "Filters": {
                "AwsAccountId": [{"Comparison": "EQUALS", "Value": self.account_id}],
                "Region": [{"Comparison": "EQUALS", "Value": self.region}],
                "UpdatedAt": [{"Start": _iso(since_ms), "End": _iso(until_ms)}],
            },
            "MaxResults": self.page_size,
        }
        tokens, identities = set(), set()
        for _ in range(200):
            check_deadline()
            page = self._call(self.client.get_findings, **kwargs)
            check_deadline()
            if (
                not isinstance(page, dict)
                or not isinstance(page.get("Findings"), list)
                or len(page["Findings"]) > self.page_size
            ):
                raise CollectionError("incomplete_findings")
            for finding in page["Findings"]:
                if (
                    not isinstance(finding, dict)
                    or finding.get("AwsAccountId") != self.account_id
                    or finding.get("Region") != self.region
                    or finding.get("SchemaVersion") != "2018-10-08"
                ):
                    raise CollectionError("finding_scope_mismatch")
                product, uid = finding.get("ProductArn"), finding.get("Id")
                # AWS products may use an empty publisher account; custom products may
                # use a publisher account distinct from the finding's affected account.
                if (
                    not isinstance(product, str)
                    or re.fullmatch(
                        ":".join(
                            (
                                "arn",
                                "aws",
                                "securityhub",
                                re.escape(self.region),
                                r"(?:[0-9]{12})?",
                                r"product/[^\s:]+",
                            )
                        ),
                        product,
                    )
                    is None
                    or not isinstance(uid, str)
                    or not 1 <= len(uid) <= 512
                    or any(ord(c) < 32 for c in uid)
                ):
                    raise CollectionError("invalid_finding_identity")
                identity = (product, uid)
                if identity in identities:
                    raise CollectionError("duplicate_finding_identity")
                identities.add(identity)
                if not since_ms <= epoch_ms(finding.get("UpdatedAt")) <= until_ms:
                    raise CollectionError("finding_window_changed")
                yield finding
            token = page.get("NextToken")
            if token is None:
                if self._hub() != before:
                    raise CollectionError("hub_changed_during_collection")
                check_deadline()
                return
            if not isinstance(token, str) or not token or len(token) > 8192 or token in tokens:
                raise CollectionError("invalid_pagination")
            tokens.add(token)
            kwargs["NextToken"] = token
        raise CollectionError("pagination_limit")
