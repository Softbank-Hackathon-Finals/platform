"""PgBouncer 관리 콘솔: PAUSE · RESUME · RELOAD와 상태 읽기, 파드 안 설정 파일의 대상 교체(spec D3).

실측(PgBouncer 1.26.1): PAUSE는 열린 트랜잭션이 끝날 때까지 응답하지 않는다. 기다리는 동안에도 상태는
paused=1이고, PAUSE를 보낸 연결을 끊어도 그대로다. 다른 연결로 RESUME해야 풀리며, 그때도 PAUSE를 보낸 연결은
응답을 받지 못한다. 그래서 시간 초과 때는 그 연결의 소켓만 끊고 RESUME하지 않는다. 시간 초과만으로 쓰기를
다시 열지 않는다(NFR-3). RESUME은 목적지가 닫혀 있음을 확인한 원상 복구 절차가 한다.
"""

import base64
import os
import re
import socket
import threading
from dataclasses import dataclass

import psycopg
from psycopg import ClientCursor

from bravo_cutover.remote import Unconfirmed
from bravo_cutover.remote.kube import Kube


class PauseTimeout(Exception):
    """deadline 안에 PAUSE가 끝나지 않았다. 일시정지는 걸린 채로 둔다(풀지 않는다)."""


@dataclass(frozen=True)
class PoolState:
    paused: bool
    host: str


class PgBouncerAdmin:
    def __init__(self, dsn: str, database: str):
        self._dsn = dsn
        self._db = database

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn, autocommit=True, cursor_factory=ClientCursor, connect_timeout=3)

    def state(self) -> PoolState:
        try:
            with self._connect() as conn:
                cur = conn.execute("SHOW DATABASES")
                cols = [d.name for d in cur.description]
                for row in cur.fetchall():
                    r = dict(zip(cols, row, strict=True))
                    if r["name"] == self._db:
                        return PoolState(paused=bool(r["paused"]), host=r["host"])
        except psycopg.Error as e:
            raise Unconfirmed(f"pgbouncer state: {e}") from e
        raise Unconfirmed(f"pgbouncer has no database {self._db}")

    def pause(self, timeout_s: float) -> None:
        conn = self._connect()
        failed: list[BaseException] = []
        done = threading.Event()

        def run():
            try:
                conn.execute(f"PAUSE {self._db}")
            except BaseException as e:  # noqa: BLE001 - 호출한 스레드로 넘긴다
                failed.append(e)
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        if not done.wait(timeout_s):
            _abort(conn)
            done.wait(2)
            raise PauseTimeout(f"PAUSE {self._db} did not finish in {timeout_s}s (left paused)")
        conn.close()
        if failed:
            raise Unconfirmed(f"PAUSE failed: {failed[0]}")

    def resume(self) -> None:
        self._command("RESUME", tolerate="not paused")

    def reload(self) -> None:
        with self._connect() as conn:
            conn.execute("RELOAD")

    def _command(self, verb: str, *, tolerate: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(f"{verb} {self._db}")
        except psycopg.Error as e:
            if tolerate not in str(e):
                raise Unconfirmed(f"{verb} failed: {e}") from e


def _abort(conn: psycopg.Connection) -> None:
    """응답을 기다리며 멈춘 연결을 끊는다. 복제한 fd로 소켓을 닫아 기다리던 스레드가 깨어나게 한다."""
    try:
        s = socket.socket(fileno=os.dup(conn.fileno()))
        s.shutdown(socket.SHUT_RDWR)
        s.close()
    except OSError:
        pass


_DB_LINE = re.compile(r"^(?P<name>[a-z_][a-z0-9_]*)\s*=\s*(?P<value>.*)$")


def retarget_ini(ini: str, database: str, host: str, port: int, dbname: str) -> str:
    """[databases] 절의 한 항목이 가리키는 host · port · dbname만 바꾼다."""
    out, section, replaced = [], "", False
    for line in ini.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            section = stripped
        m = _DB_LINE.match(stripped)
        if section == "[databases]" and m and m["name"] == database:
            line = f"{database} = host={host} port={port} dbname={dbname}"
            replaced = True
        out.append(line)
    if not replaced:
        raise ValueError(f"no [databases] entry for {database}")
    return "\n".join(out) + "\n"


class PgBouncerConfig:
    """파드 안의 쓰기 가능한 설정 파일(emptyDir)을 바꾼다. RELOAD는 관리 콘솔로 따로 한다."""

    def __init__(self, kube: Kube, namespace: str, selector: str, path: str):
        self._kube, self._ns, self._selector, self._path = kube, namespace, selector, path

    def pod(self) -> str:
        pods = self._kube.running_pods(self._ns, self._selector)
        # 관리 콘솔은 Service 하나로 붙는다. 파드가 여럿이면 일부만 PAUSE되므로 거부한다.
        if len(pods) != 1:
            raise Unconfirmed(f"expected exactly one pgbouncer pod, found {pods}")
        return pods[0]

    def read(self) -> str:
        return self._kube.exec(self._ns, self.pod(), ["cat", self._path])

    def retarget(self, database: str, host: str, port: int, dbname: str) -> None:
        pod = self.pod()
        new = retarget_ini(self._kube.exec(self._ns, pod, ["cat", self._path]), database, host, port, dbname)
        encoded = base64.b64encode(new.encode()).decode()
        self._kube.exec(self._ns, pod, [
            "sh", "-c", f'echo "{encoded}" | base64 -d > "$1.tmp" && mv "$1.tmp" "$1"', "sh", self._path,
        ])
        if self._kube.exec(self._ns, pod, ["cat", self._path]) != new:
            raise Unconfirmed("pgbouncer.ini was not written")
