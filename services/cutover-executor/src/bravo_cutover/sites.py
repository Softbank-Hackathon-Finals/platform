"""환경(출발지 · 목적지)마다 Executor가 다루는 대상의 위치 · 접속 정보와 실행 설정.

접속 문자열에는 비밀번호가 들어 있으므로 파일은 플랫폼에만 두고 로그 · 응답에 내보내지 않는다.
모의 환경은 testing/mockenv.py가 testing/.state/sites.json을 만든다.
"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class Site(BaseModel):
    model_config = ConfigDict(extra="forbid")

    environment_id: str
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
    # 업무 쓰기를 하지 않는 역할. 펜싱 때 이 밖의 세션이 남아 있으면 우회 쓰기로 본다.
    system_roles: list[str] = ["cutover_control", "replicator", "subscriber", "postgres"]


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_lag_bytes: int = 1024 * 1024
    long_transaction_ms: int = 200
    pause_timeout_s: float = 3
    barrier_timeout_s: float = 10
    fence_timeout_s: float = 3
    # 비교 대상과 시퀀스(올리기만). 시퀀스는 {이름: [테이블, 열]}
    tables: list[str] = ["public.attachments", "public.messages"]
    sequences: dict[str, tuple[str, str]] = {"public.messages_id_seq": ("public.messages", "id")}
    # T13 연결 전 임시 비교(UNVERIFIED)를 통과로 볼지. 모의 환경에서만 켠다(spec D11).
    allow_unverified_compare: bool = False


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: Settings = Settings()
    sites: dict[str, Site]


def load_config(path: str | Path) -> Config:
    return Config.model_validate(json.loads(Path(path).read_text()))
