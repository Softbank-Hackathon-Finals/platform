import asyncio
import json
import time
from collections.abc import Callable

import boto3


def fetch_db_credentials(secret_id: str) -> tuple[str, str]:
    """DB 사용자와 비밀번호를 Secrets Manager에서 직접 읽는다.

    플랫폼이 찾아서 OpenBao · ESO 주입으로 고칠 "코드 수정" 등급 의존성으로 일부러 남긴 호출이다(설계 문서 8.10).
    미리 추상화하거나 환경변수로 바꾸지 않는다.
    """
    client = boto3.client("secretsmanager")
    secret = json.loads(client.get_secret_value(SecretId=secret_id)["SecretString"])
    return secret["username"], secret["password"]


class DbCredentials:
    """새 DB 연결마다 쓸 자격 증명. 회전에 대비해 주기적으로, 인증 실패 시에는 즉시 다시 읽는다.

    기존 연결은 비밀번호가 바뀌어도 유지되므로, 이 캐시는 새 연결에만 영향을 준다.
    """

    def __init__(self, fetch: Callable[[], tuple[str, str]], ttl_seconds: float = 300):
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._value: tuple[str, str] | None = None
        self._fetched_at = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> tuple[str, str]:
        async with self._lock:
            if self._value is None or time.monotonic() - self._fetched_at > self._ttl:
                self._value = await asyncio.to_thread(self._fetch)
                self._fetched_at = time.monotonic()
            return self._value

    def invalidate(self) -> None:
        self._value = None
