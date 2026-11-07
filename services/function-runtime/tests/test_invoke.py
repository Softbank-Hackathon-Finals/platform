import json
import os
import time

from fakes import FakeQueue

from bravo_function_runtime import event
from bravo_function_runtime.config import Binding
from bravo_function_runtime.invoke import Kind, child_env, invoke

HANDLERS = os.path.join(os.path.dirname(__file__), "handlers")


def _binding(handler, **kw):
    return Binding(function_name="pii-check", handler=f"sample.{handler}", queue_name="q",
                   handler_path=HANDLERS, **kw)


def _call(b, tmp_path, monkeypatch, timeout_s=None):
    monkeypatch.setenv("RESULT_FILE", str(tmp_path / "out.jsonl"))
    q = FakeQueue(bodies=['{"idempotency_key": "k1"}'])
    ctx = event.context(b, "ap-northeast-1", "000000000000", int((time.time() + b.timeout_s) * 1000))
    return invoke(b, event.event(q.receive(10, 0), q.arn(), "ap-northeast-1"), ctx, "ap-northeast-1")


def test_unmodified_handler_runs_with_lambda_event_and_context(tmp_path, monkeypatch):
    out = _call(_binding("ok", pass_env=("RESULT_FILE",)), tmp_path, monkeypatch)
    assert out.kind == Kind.OK and "handled 1" in out.logs
    row = json.loads((tmp_path / "out.jsonl").read_text())
    assert row["key"] == "k1" and row["function"] == "pii-check" and 0 < row["remaining_ms"] <= 3000


def test_handler_exception_is_an_error_outcome(tmp_path, monkeypatch):
    out = _call(_binding("boom"), tmp_path, monkeypatch)
    assert out.kind == Kind.ERROR and "ValueError: bad message" in out.error


def test_timeout_kills_the_handler(tmp_path, monkeypatch):
    start = time.monotonic()
    out = _call(_binding("slow", timeout_s=1), tmp_path, monkeypatch)
    assert out.kind == Kind.TIMEOUT and time.monotonic() - start < 5


def test_runtime_secrets_are_not_passed_to_the_handler():
    env = child_env(_binding("leak", pass_env=("DB_DSN",)), "ap-northeast-1",
                    {"PATH": "/bin", "ADMIN_TOKEN": "t", "DB_DSN": "postgres://x", "AWS_SECRET_ACCESS_KEY": "s"})
    assert env["DB_DSN"] == "postgres://x"
    assert "ADMIN_TOKEN" not in env and "AWS_SECRET_ACCESS_KEY" not in env


def _gone(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as f:
            return any(line.startswith("State:") and "Z" in line for line in f)
    except FileNotFoundError:
        return True


def test_timeout_also_kills_grandchildren_without_waiting_for_them(tmp_path, monkeypatch):
    start = time.monotonic()
    out = _call(_binding("spawn_and_hang", timeout_s=1, pass_env=("RESULT_FILE",)), tmp_path, monkeypatch)
    assert out.kind == Kind.TIMEOUT and time.monotonic() - start < 4
    grandchild = int((tmp_path / "out.jsonl").read_text())
    deadline = time.monotonic() + 3
    while not _gone(grandchild) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _gone(grandchild)


def test_handler_output_is_capped_and_never_held_in_memory(tmp_path, monkeypatch):
    out = _call(_binding("noisy", timeout_s=20), tmp_path, monkeypatch)
    # Python은 파일 크기 상한 신호를 무시하고 쓰기 오류(EFBIG)로 바꾼다. 출력이 상한에서 멈추면 실패로 끝난다.
    assert out.kind in (Kind.ERROR, Kind.CRASH) and len(out.logs) <= 4000
    assert out.kind == Kind.CRASH or "File too large" in out.error
