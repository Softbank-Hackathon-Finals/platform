"""대조에 쓸 행을 DB에서 읽는다. 읽기 전용 트랜잭션만 연다."""

import psycopg
from psycopg.rows import dict_row

from bravo_loadgen.journal import message_author

_ROWS_SQL = """
SELECT id, idempotency_key::text AS idempotency_key, author, body
FROM messages
WHERE author = %(author)s OR body LIKE %(marker)s OR idempotency_key = ANY(%(keys)s::uuid[])
ORDER BY id
"""


def fetch_rows(dsn: str, run_id: str, logical_ids: list[str], author: str | None = None) -> list[dict]:
    """이번 실행이 남겼을 수 있는 행을 모두 읽는다.

    멱등 키만으로 찾으면 다른 키로 들어간 복사본을, 작성자만으로 찾으면 내용이 바뀐 행을 놓친다.
    작성자로는 이번 실행만 쓰는 이름(loadgen-<run_id>)일 때만 찾는다. 로그인을 켠 실행(T47)의 계정 이름은
    다른 실행의 행까지 끌어와 유령 행으로 센다. 그때는 키와 본문 표시로만 찾는다(NULL은 어느 행과도 같지 않다).
    """
    own = message_author(run_id)
    by_author = own if author in (None, own) else None
    with psycopg.connect(dsn, prepare_threshold=None, row_factory=dict_row) as connection:
        connection.read_only = True
        return connection.execute(
            _ROWS_SQL,
            {
                "author": by_author,
                "marker": f"t18 run={run_id} req=%",
                "keys": logical_ids,
            },
        ).fetchall()
