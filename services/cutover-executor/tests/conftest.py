"""실행 기록 테스트용 Postgres.

EXECUTOR_TEST_DSN이 있으면 그 DB를 쓰고, 없으면 docker로 postgres:16을 잠깐 띄운다.
둘 다 없으면 건너뛴다(사유를 출력한다). GitHub Actions의 ubuntu 러너에는 docker가 있어 CI에서도 돈다.
"""

import os
import re
import shutil
import subprocess
import time
import uuid
import warnings
from contextlib import contextmanager

import psycopg
import pytest

IMAGE = os.environ.get("EXECUTOR_TEST_POSTGRES_IMAGE", "postgres:16")
OTHER_MAJOR_IMAGES = {16: "public.ecr.aws/docker/library/postgres:18.4", 18: "public.ecr.aws/docker/library/postgres:16"}
# 공개 레지스트리는 익명 요청을 제한한다(429). 받지 못하면 docker run은 코드 125로 바로 끝나지만 제한은 곧 풀린다.
# 뒤쪽의 연결 오류는 재현하지 않았고, 같은 성격의 일시 오류로 함께 다시 시도한다.
TRANSIENT_DOCKER_ERROR = re.compile(
    r"rate exceeded|too many requests|toomanyrequests|\b429\b"
    r"|timeout|context deadline exceeded|connection reset|\beof\b|temporary failure|\b50[234]\b",
    re.IGNORECASE,
)
DOCKER_RUN_RETRY_DELAYS_S = (2, 5, 10, 20)


def docker_run(args: list[str], image: str) -> None:
    """실패하면 docker stderr를 그대로 보여 준다. 인자에는 비밀번호가 있어 메시지에 넣지 않는다."""
    for delay in (*DOCKER_RUN_RETRY_DELAYS_S, None):
        result = subprocess.run(["docker", "run", *args, image], capture_output=True, text=True)
        if result.returncode == 0:
            return
        error = result.stderr.strip()
        if delay is None or not TRANSIENT_DOCKER_ERROR.search(error):
            raise RuntimeError(f"docker run {image} 실패(종료 코드 {result.returncode}): {error}")
        warnings.warn(f"docker run {image} 일시 오류, {delay}초 뒤 다시 시도: {error}", stacklevel=2)
        time.sleep(delay)


@contextmanager
def postgres_container(image: str):
    name, password = f"cutover-test-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    # 포트는 docker가 고르게 한다(미리 고른 빈 포트가 그 사이에 잡히면 실행이 실패한다)
    docker_run(["-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={password}", "-p", "127.0.0.1::5432"], image)
    port = subprocess.run(["docker", "port", name, "5432/tcp"], check=True, capture_output=True,
                          text=True).stdout.split(":")[-1].strip()
    dsn = f"host=127.0.0.1 port={port} user=postgres password={password} dbname=postgres"
    try:
        deadline = time.monotonic() + 60
        while True:
            try:
                psycopg.connect(dsn, connect_timeout=2).close()
                break
            except psycopg.OperationalError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.5)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)


@pytest.fixture(scope="session")
def postgres_dsn():
    if dsn := os.environ.get("EXECUTOR_TEST_DSN"):
        yield dsn
        return
    if not shutil.which("docker"):
        pytest.skip("EXECUTOR_TEST_DSN도 docker도 없다: 실행 기록 테스트를 돌릴 수 없다")
    with postgres_container(IMAGE) as dsn:
        yield dsn


@pytest.fixture(scope="session")
def other_major_dsn(postgres_dsn):
    """postgres_dsn과 메이저 버전이 다른 서버. 지원하는 두 메이저(16 · 18)를 실제로 맞붙인다."""
    if dsn := os.environ.get("EXECUTOR_TEST_OTHER_MAJOR_DSN"):
        yield dsn
        return
    if not shutil.which("docker"):
        pytest.skip("EXECUTOR_TEST_OTHER_MAJOR_DSN도 docker도 없다: 메이저 버전 시험을 돌릴 수 없다")
    with psycopg.connect(postgres_dsn) as conn:
        major = conn.info.server_version // 10000
    with postgres_container(OTHER_MAJOR_IMAGES[major]) as dsn:
        yield dsn


@pytest.fixture
def store(postgres_dsn):
    from bravo_cutover.store import Store

    with psycopg.connect(postgres_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS executor CASCADE")
    s = Store(postgres_dsn)
    s.migrate()
    yield s
    s.close()
