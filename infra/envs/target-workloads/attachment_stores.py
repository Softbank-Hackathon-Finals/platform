"""S3 and SeaweedFS adapters for sync_attachments.synchronize().

Listings and reads are capped so neither side can pull more than the transfer
limits into memory. The source adapter has no write method at all. Destination
create is a conditional write, so the store itself refuses an existing key even
if an object appeared after the inventory was listed.
"""
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from sync_attachments import MAX_BYTES, MAX_OBJECTS

PRECONDITION_FAILED = 412


class _BoundedBucket:
    def __init__(self, client, bucket, **request):
        self._client = client
        self._bucket = bucket
        self._request = request

    def sizes(self):
        """Map key to size, stopping past the object limit; an oversized bucket stays a mismatch."""
        found, continuation = {}, {}
        while len(found) <= MAX_OBJECTS:
            page = self._client.list_objects_v2(Bucket=self._bucket, MaxKeys=MAX_OBJECTS + 1,
                                                **continuation, **self._request)
            found.update((item["Key"], item["Size"]) for item in page.get("Contents", ()))
            if not page.get("IsTruncated"):
                break
            continuation = {"ContinuationToken": page["NextContinuationToken"]}
        return found

    def list_keys(self):
        return list(self.sizes())

    def read(self, key):
        """Return the bytes, at most one past the size limit so synchronize() can reject them."""
        response = self._client.get_object(Bucket=self._bucket, Key=key, **self._request)
        with response["Body"] as body:
            content = body.read(MAX_BYTES + 1)
        # A bounded read skips the SDK's length check, so a cut stream must not pass as the object.
        if len(content) < min(response["ContentLength"], MAX_BYTES + 1):
            raise ValueError("incomplete object read")
        return content


class SourceStore(_BoundedBucket):
    """Read-only by construction: list and read are the whole surface."""


class DestinationStore(_BoundedBucket):
    def create(self, key, content):
        try:
            self._client.put_object(Bucket=self._bucket, Key=key, Body=content, IfNoneMatch="*")
        except ClientError as error:
            if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") == PRECONDITION_FAILED:
                raise ValueError("destination conflict; existing object is preserved") from None
            raise


def _client_config(addressing_style):
    return Config(signature_version="s3v4", s3={"addressing_style": addressing_style},
                  connect_timeout=10, read_timeout=20, retries={"max_attempts": 1},
                  request_checksum_calculation="when_required",
                  response_checksum_validation="when_required")


def source_store(bucket, region, expected_owner=None, endpoint_url=None):
    """Credentials come from the SDK default chain (role, IRSA or the operator's profile)."""
    client = boto3.client("s3", region_name=region, endpoint_url=endpoint_url,
                          config=_client_config("path" if endpoint_url else "auto"))
    request = {"ExpectedBucketOwner": expected_owner} if expected_owner else {}
    return SourceStore(client, bucket, **request)


def destination_store(endpoint_url, bucket, access_key_id, secret_access_key):
    """Local signing keys only: these are not AWS keys, so the default chain must stay unused."""
    client = boto3.client("s3", endpoint_url=endpoint_url, region_name="us-east-1",
                          aws_access_key_id=access_key_id, aws_secret_access_key=secret_access_key,
                          aws_session_token=None, config=_client_config("path"))
    return DestinationStore(client, bucket)
