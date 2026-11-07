"""환경(출발지 · 목적지)마다 Executor가 다루는 대상의 위치 · 접속 정보와 실행 설정.

접속 문자열에는 비밀번호가 들어 있으므로 파일은 플랫폼에만 두고 로그 · 응답에 내보내지 않는다.
모의 환경은 testing/mockenv.py가 testing/.state/sites.json을 만든다.
"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Kubernetes 객체 이름(DNS-1123 label). CNPG Cluster · 네임스페이스 · 풀러 Deployment 이름에 쓴다.
K8S_NAME = r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"


class Site(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment_id: str
    # 환경 종류(aws · gcp · onprem 등, docs/contracts/environment-kinds.json). 기록용이다(ADR-0034).
    # Executor는 판정 · 실행에 쓰지 않는다: 공통 기반(K8s · CNPG · PgBouncer)만 다루므로 종류를 몰라도 된다.
    # 새 종류가 계약에 추가돼도 Executor를 바꾸지 않도록 값 목록을 고정하지 않는다.
    kind: str | None = Field(None, pattern=r"^[a-z][a-z0-9-]{0,31}$")
    # 실제 DB 준비 패치(prepare.py)가 확인하는 CNPG Cluster 이름 · 그 네임스페이스 · 풀러 Deployment 이름.
    # 비우면 기존 배포 규칙 {side}-db · {side}-dev · {side}-pgbouncer를 쓴다(side는 source | target).
    db_cluster: str | None = Field(None, pattern=K8S_NAME)
    db_namespace: str | None = Field(None, pattern=K8S_NAME)
    pgbouncer_deployment: str | None = Field(None, pattern=K8S_NAME)
    # 실제 DB 자원 ID와 허용 접속 참조(resources.Registry). lease · epoch는 resource_id마다 걸린다.
    resource_id: str
    connection_ref: str
    kube_context: str
    namespace: str
    app_selector: str = "app=demo-chat"
    pgbouncer_selector: str = "app=pgbouncer"
    pgbouncer_ini: str = "/etc/pgbouncer/pgbouncer.ini"
    pgbouncer_configmap: str = "pgbouncer-config"
    pgbouncer_configmap_key: str = "pgbouncer.ini"
    pgbouncer_database: str = Field("chat", pattern=r"^[a-z_][a-z0-9_]*$")
    # PgBouncer 관리 콘솔(cutover_control), PgBouncer를 거친 앱 역할 연결(probe)
    pgbouncer_admin_dsn: str = Field(repr=False)
    pgbouncer_app_dsn: str = Field(repr=False)
    # DB 직접 연결: 제어 역할, 앱 역할(펜싱 시험)
    db_control_dsn: str = Field(repr=False)
    db_app_dsn: str = Field(repr=False)
    # PgBouncer 설정의 host= 값으로 쓸 이 환경 DB의 주소
    db_host: str
    db_port: int = 5432
    db_name: str = "chat"
    app_role: str = "chat_app"
    # 이 DB에 있는, 상대 환경에서 받아 오는 구독과 그 소유 역할
    subscription: str
    subscription_owner: str = "subscriber"
    # Actual clusters use fixed-target control functions; no SET ROLE to postgres.
    control_functions: bool = False
    # 업무 쓰기를 하지 않는 역할. 펜싱 때 이 밖의 세션이 남아 있으면 우회 쓰기로 본다.
    system_roles: list[str] = ["cutover_control", "replicator", "subscriber", "postgres"]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_lag_bytes: int = 1024 * 1024
    long_transaction_ms: int = 200
    pause_timeout_s: float = 3
    barrier_timeout_s: float = 10
    # 사전 확인의 복제 경로 왕복 시험 대기(ADR-0016). 실제 값은 T8 복제 지연 실측 뒤 정한다.
    path_probe_timeout_s: float = 10
    fence_timeout_s: float = 3
    # 복구 뒤 출발지 쓰기 확인을 기다리는 시간. PgBouncer는 서버 로그인 실패를 server_login_retry(기본 15초)
    # 동안 기억하고 바로 거절하므로, 펜싱 중 로그인을 시도한 PgBouncer는 복구 직후 잠시 쓰기를 거절한다.
    restore_verify_timeout_s: float = 20
    # 비교 대상과 시퀀스(올리기만). 시퀀스는 {이름: [테이블, 열]}
    tables: list[str] = ["public.attachments", "public.messages"]
    sequences: dict[str, tuple[str, str]] = {"public.messages_id_seq": ("public.messages", "id")}


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: Settings = Settings()
    sites: dict[str, Site]
    # 이전 전 주본이던 환경(원래 출발지). 요청의 목적지가 이 환경이면 되돌리기다(영수증 direction, T20).
    origin: str

    @model_validator(mode="after")
    def _origin_is_registered(self) -> "Config":
        if self.origin not in self.sites:
            raise ValueError(f"origin {self.origin!r} is not a registered environment")
        return self

    def direction(self, source: str, target: str) -> str:
        """영수증의 방향. 원래 출발지에서 나가면 cutover, 원래 출발지로 돌아오면 failback."""
        if source == self.origin and target != self.origin:
            return "cutover"
        if target == self.origin and source != self.origin:
            return "failback"
        raise ValueError(f"request {source} -> {target} does not leave or return to origin {self.origin}")

    def deployment_registry(self):
        if len(self.sites) != 2 or any(k != s.environment_id for k, s in self.sites.items()):
            raise ValueError("the deployed executor requires one fixed pair of environments")
        if len({s.resource_id for s in self.sites.values()}) != 2:
            raise ValueError("deployment requires two distinct database resources")
        return registry_from_sites(self.sites, origin=self.origin)


def load_config(path: str | Path) -> Config:
    return Config.model_validate(json.loads(Path(path).read_text()))


def registry_from_sites(sites: dict[str, Site], origin: str | None = None):
    """실행기 설정의 환경 목록으로 자원 등록을 만든다(설정 파일 하나로 실행기와 API가 같은 등록을 쓴다).
    origin은 설정의 원래 출발지다. 등록 조회가 이 값을 source로 공개한다."""
    from bravo_cutover.resources import Registry, Resource

    return Registry([Resource(environment_id=s.environment_id, resource_id=s.resource_id,
                              connection_ref=s.connection_ref) for s in sites.values()], origin=origin)
