"""서버에 등록한 실제 DB 자원 (#115 리뷰: lease를 호출자가 준 이름이 아니라 실제 자원에 묶는다).

요청의 environment_id와 connection_ref를 이 등록 정보로 확인하고, 실제 DB 자원 ID로 바꾼다.
등록되지 않은 환경, 접속 참조 불일치, 출발지와 목적지가 같은 자원이면 원격 조작 없이 거부한다.
lease와 epoch는 이 자원 ID 하나하나에 걸린다(store). 그래서 별칭으로 같은 DB를 가리키거나
한쪽 DB만 공유하는 요청도 겹치는 실행으로 막힌다.
"""

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from bravo_cutover.contract import IDENTIFIER_PATTERN, CutoverRequest, Endpoint
from bravo_cutover.rejections import ConnectionRefMismatch, SameResource, UnknownEnvironment


class Resource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment_id: str
    # 실제 DB를 가리키는 서버 쪽 식별자(예: cnpg-aws/chat). 같은 DB면 환경 이름이 달라도 같은 값이다.
    resource_id: str = Field(min_length=1, max_length=256)
    # 이 환경에 허용하는 접속 정보 참조. 요청의 connection_ref와 같아야 한다.
    connection_ref: str = Field(min_length=1, max_length=256)


class RegisteredPair(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Resource
    target: Resource


class Registry:
    def __init__(self, resources: list[Resource], *, origin: str | None = None):
        # 원래 출발지(설정의 origin). 등록 조회의 source와 영수증 direction이 같은 값을 쓴다(T20).
        # 모르면(자원 파일만 읽는 기록 전용 서버) 등록 쌍을 공개하지 않는다.
        self.origin = origin
        self._by_env: dict[str, Resource] = {}
        for r in resources:
            if r.environment_id in self._by_env:
                raise ValueError(f"duplicate environment {r.environment_id}")
            self._by_env[r.environment_id] = r

    @classmethod
    def from_file(cls, path: str | Path) -> "Registry":
        """{"env-aws": {"resource_id": "...", "connection_ref": "..."}, ...}"""
        raw = json.loads(Path(path).read_text())
        return cls([Resource(environment_id=env, **value) for env, value in raw.items()])

    def public_pair(self) -> RegisteredPair | None:
        """Return the two registered deployment resources: the configured origin is the source.
        Environment names are not fixed, so a cloud-to-cloud pair is projected the same way."""
        if self.origin is None or len(self._by_env) != 2 or self.origin not in self._by_env:
            return None
        if not all(re.fullmatch(IDENTIFIER_PATTERN, env) for env in self._by_env):
            return None
        source = self._by_env[self.origin]
        [target] = [r for env, r in self._by_env.items() if env != self.origin]
        # A URI or a key=value DSN in a public identifier is a configuration error.
        public_id = r"[A-Za-z0-9_.:-]+(?:/[A-Za-z0-9_.:-]+)*"
        if source.resource_id == target.resource_id or not all(
            re.fullmatch(public_id, value)
            for resource in (source, target)
            for value in (resource.resource_id, resource.connection_ref)
        ):
            return None
        return RegisteredPair(source=source, target=target)

    def _resource(self, endpoint: Endpoint) -> str:
        resource = self._by_env.get(endpoint.environment_id)
        if resource is None:
            raise UnknownEnvironment(f"environment {endpoint.environment_id} is not registered")
        if endpoint.connection_ref != resource.connection_ref:
            raise ConnectionRefMismatch(f"connection_ref does not match the registered one for {endpoint.environment_id}")
        return resource.resource_id

    def resolve(self, request: CutoverRequest) -> tuple[str, str]:
        """(출발지 자원 ID, 목적지 자원 ID)."""
        source, target = self._resource(request.source), self._resource(request.target)
        if source == target:
            raise SameResource(f"source and target are the same resource {source}")
        return source, target
