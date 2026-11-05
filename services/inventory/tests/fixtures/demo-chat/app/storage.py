import boto3
from botocore.config import Config


class AttachmentStorage:
    """첨부파일을 S3 SDK로 저장한다. 엔드포인트만 바꾸면 S3 호환 저장소(SeaweedFS 등)로 옮겨진다("설정 교체" 등급)."""

    def __init__(self, bucket: str, endpoint_url: str | None, force_path_style: bool):
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            config=Config(s3={"addressing_style": "path" if force_path_style else "auto"}),
        )

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
