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


def fetch_rows(dsn: str, run_id: str, logical_ids: list[str]) -> list[dict]:
    """이번 실행이 남겼을 수 있는 행을 모두 읽는다.

    멱등 키만으로 찾으면 다른 키로 들어간 복사본을, 작성자만으로 찾으면 내용이 바뀐 행을 놓친다.
    """
    with psycopg.connect(dsn, prepare_threshold=None, row_factory=dict_row) as connection:
        connection.read_only = True
        return connection.execute(
            _ROWS_SQL,
            {
                "author": message_author(run_id),
                "marker": f"t18 run={run_id} req=%",
                "keys": logical_ids,
            },
        ).fetchall()
