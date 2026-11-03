import pytest

from bravo_cutover.remote.pgbouncer import retarget_ini

INI = """[databases]
chat = host=postgres.src.svc port=5432 dbname=chat
other = host=keep port=5432 dbname=other

[pgbouncer]
listen_port = 6432
chat = not-a-database-line
"""


def test_only_the_named_database_entry_changes():
    out = retarget_ini(INI, "chat", "postgres.dst.svc", 5432, "chat")
    assert "chat = host=postgres.dst.svc port=5432 dbname=chat" in out
    assert "other = host=keep port=5432 dbname=other" in out
    assert "chat = not-a-database-line" in out  # [pgbouncer] 절은 건드리지 않는다


def test_missing_entry_is_an_error():
    with pytest.raises(ValueError):
        retarget_ini(INI, "missing", "h", 5432, "d")
