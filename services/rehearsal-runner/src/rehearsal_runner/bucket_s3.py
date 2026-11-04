"""Read-only S3 clients for independent bucket observations.

The caller supplies a trusted session and a rehearsal-only endpoint. The job's
network and credentials must enforce that scope; this module never applies a
policy or creates, changes, or deletes a bucket or object.
"""

from dataclasses import dataclass
from urllib.parse import urlsplit

from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

from .bucket_probe import ReadResult


@dataclass(frozen=True)
class S3ObjectReader:
    client: object

    def get(self, bucket: str, key: str) -> ReadResult:
        try:
            response = self.client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            return ReadResult(status if type(status) is int else 0)
        status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        body = response.get("Body")
        if type(status) is not int:
            if body is not None:
                body.close()
            return ReadResult(0)
        return ReadResult(status, body)


def bucket_readers(session, *, endpoint_url: str, region: str) -> tuple[S3ObjectReader, S3ObjectReader]:
    """Return separately signed and explicitly unsigned readers for one endpoint.

    This function accepts no model input and does not authorize access. In
    particular, the caller must ensure endpoint and credentials belong to the
    isolated test. Client errors are reduced to status codes, never logged.
    """
    parsed = urlsplit(endpoint_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or not isinstance(region, str) or not region.strip()):
        raise ValueError("invalid S3 endpoint configuration")
    common = dict(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0},
                  proxies={}, s3={"addressing_style": "path"})
    signed = session.client("s3", endpoint_url=endpoint_url, region_name=region,
                            config=Config(signature_version="s3v4", **common))
    unsigned = session.client("s3", endpoint_url=endpoint_url, region_name=region,
                              config=Config(signature_version=UNSIGNED, **common))
    return S3ObjectReader(signed), S3ObjectReader(unsigned)
