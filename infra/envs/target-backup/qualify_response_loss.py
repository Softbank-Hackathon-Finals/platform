"""Exercise lost replies against the disposable TLS SeaweedFS in qualify_local.sh.

The original S3 request completes before the reply is hidden from verify.check.
No team bucket or credentials are used. Invoke only from the local qualification harness.
"""

from unittest.mock import patch

import verify
from botocore.exceptions import ClientError, ReadTimeoutError
from render import BUCKET


def main():
    endpoint, ca = "https://seaweedfs:8333", "/w/ca.crt"
    labels = ["app", "executor"]
    original_client = verify.client
    admin = original_client(endpoint, "admin", ca)
    checked = 0
    for disposable in (False, True):
        healthy = verify.check(endpoint, labels, disposable, ca)
        assert verify.status(healthy, labels, disposable) == "PASS"
        checked += 1
        for fault in ("reader_delete", "writer_put"):
            injected = []

            def client(address, label, certificate, fault=fault, injected=injected):
                real = original_client(address, label, certificate)

                class ReplyLoss:
                    def __getattr__(self, name):
                        operation = getattr(real, name)

                        def call(**kwargs):
                            if fault == "reader_delete" and label == "reader" and name == "delete_object" and "VersionId" in kwargs:
                                try:
                                    operation(**kwargs)
                                except ClientError as error:
                                    assert error.response["Error"]["Code"] == "AccessDenied"
                                    injected.append(name)
                                    raise ReadTimeoutError(endpoint_url=endpoint) from None
                                raise AssertionError("expected real version-delete refusal")
                            result = operation(**kwargs)
                            if fault == "writer_put" and label == "writer" and name == "put_object":
                                assert result.get("VersionId")
                                injected.append(name)
                                raise ReadTimeoutError(endpoint_url=endpoint)
                            return result

                        return call

                return ReplyLoss()

            with patch.object(verify, "client", client):
                out = verify.check(endpoint, labels, disposable, ca)
            assert len(injected) == 1
            assert verify.status(out, labels, disposable) == "UNKNOWN"
            versions = admin.list_object_versions(Bucket=BUCKET, Prefix=out["probe_left"])
            exact = [v for v in versions.get("Versions", []) if v["Key"] == out["probe_left"]]
            assert len(exact) == 1 and not versions.get("DeleteMarkers")
            body = admin.get_object(Bucket=BUCKET, Key=out["probe_left"], VersionId=exact[0]["VersionId"])["Body"]
            try:
                assert body.read() == b"t35"
            finally:
                body.close()
            if fault == "reader_delete":
                assert out["probe_intact"] is True
                assert "reader" in verify.scope(out, labels, disposable)[1]
            else:
                assert out["probe_intact"] is None
                assert "reader.put" not in out  # no further write after the lost PUT reply
            checked += 1

    policy_reads = []

    def lost_final_read(address, label, certificate):
        real = original_client(address, label, certificate)
        if label != "admin":
            return real
        original = real.get_bucket_policy

        def get_policy(**kwargs):
            result = original(**kwargs)
            policy_reads.append(True)
            if len(policy_reads) == 2:
                raise ReadTimeoutError(endpoint_url=endpoint)
            return result

        real.get_bucket_policy = get_policy
        return real

    with patch.object(verify, "client", lost_final_read):
        out = verify.check(endpoint, labels, True, ca)
    assert out["protection_after"] == {"versioning_enabled": True, "policy_matches": None}
    assert verify.status(out, labels, True) == "UNKNOWN"
    assert verify.protection(admin) == {"versioning_enabled": True, "policy_matches": True}
    checked += 1
    print(f"TLS SeaweedFS response-loss qualification: {checked}/{checked} PASS; no retries, original versions retained")


if __name__ == "__main__":
    main()
