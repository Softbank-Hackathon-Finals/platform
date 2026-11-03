"""환경(출발지 · 목적지)마다 Executor가 다루는 대상의 위치와 접속 정보.

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
    subscription_owner: str = "subscriber"


def load_sites(path: str | Path) -> dict[str, Site]:
    raw = json.loads(Path(path).read_text())
    return {key: Site.model_validate(value) for key, value in raw.items()}
