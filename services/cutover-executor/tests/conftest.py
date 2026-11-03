"""실행 기록 테스트용 Postgres.

EXECUTOR_TEST_DSN이 있으면 그 DB를 쓰고, 없으면 docker로 postgres:16을 잠깐 띄운다.
둘 다 없으면 건너뛴다(사유를 출력한다). GitHub Actions의 ubuntu 러너에는 docker가 있어 CI에서도 돈다.
"""

import os
import shutil
import subprocess
import time
import uuid

import psycopg
import pytest

IMAGE = os.environ.get("EXECUTOR_TEST_POSTGRES_IMAGE", "postgres:16")


@pytest.fixture(scope="session")
def postgres_dsn():
    if dsn := os.environ.get("EXECUTOR_TEST_DSN"):
        yield dsn
        return
    if not shutil.which("docker"):
        pytest.skip("EXECUTOR_TEST_DSN도 docker도 없다: 실행 기록 테스트를 돌릴 수 없다")
    name, password = f"cutover-test-{uuid.uuid4().hex[:8]}", uuid.uuid4().hex
    # 포트는 docker가 고르게 한다(미리 고른 빈 포트가 그 사이에 잡히면 실행이 실패한다)
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={password}",
         "-p", "127.0.0.1::5432", IMAGE],
        check=True, capture_output=True,
    )
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


@pytest.fixture
def store(postgres_dsn):
    from bravo_cutover.store import Store

    with psycopg.connect(postgres_dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS executor CASCADE")
    s = Store(postgres_dsn)
    s.migrate()
    yield s
    s.close()
