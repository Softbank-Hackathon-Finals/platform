"""명령줄: 실제 소켓으로 끝까지 돌려 산출물과 종료 코드를 본다. DB가 필요한 시험은 test_db.py."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from bravo_loadgen.cli import DEFAULT_WINDOW_SECONDS, main, open_files_needed

PROFILE = json.loads(
    (Path(__file__).resolve().parents[3] / "docs/observability/threshold-profiles.json").read_text()
)["profiles"][0]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    env: str | None = "aws"

    def log_message(self, *args):
        pass

    def _send(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        if self.env:
            self.send_header("X-Bravo-Env", self.env)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self._send(200, [])

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/windows":
            self.server.windows.append(json.loads(body))
            return self._send(202, {})
        if self.path != "/api/messages":
            return self._send(404, {})
        self.server.count += 1
        self._send(201, {"id": self.server.count})


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.count = 0
    httpd.windows = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield httpd
    httpd.shutdown()


def args(server, out, *extra):
    return [
        "run", "--base-url", getattr(server, "url", server), "--plan-id", "plan-demo",
        "--source-env", "aws", "--target-env", "onprem",
        "--duration", "1.2", "--write-rps", "10", "--read-rps", "10", "--window-seconds", "1",
        "--measurement-path", "local-direct", "--out", str(out), "--run-id", "t18-cli",
        "--attempt-timeout", "2", *extra,
    ]


def test_window_seconds_default_matches_threshold_profile():
    assert DEFAULT_WINDOW_SECONDS == PROFILE["window_seconds"]


def test_invalid_window_url_stops_before_sending_load(server, tmp_path, capsys):
    assert main(args(server, tmp_path, "--window-url", "not a url")) == 2
    assert "--window-url" in capsys.readouterr().err
    assert not (tmp_path / "journal.jsonl").exists()


def test_run_without_a_database_is_unknown_and_exits_two(server, tmp_path):
    assert main(args(server, tmp_path)) == 2
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["verdict"] == "UNKNOWN" and report["reasons"] == ["NOT_RECONCILED"]
    assert report["windows"]["emitted"] == 1 and report["windows"]["conserved"] is True
    assert report["load"]["achieved_write_rps"] == pytest.approx(10, abs=0.5)
    assert "local-direct" in (tmp_path / "report.md").read_text()
    assert (tmp_path / "journal.jsonl").is_file()
    assert report["journal_sha256"]
    windows = [json.loads(line) for line in (tmp_path / "windows.jsonl").read_text().splitlines()]
    assert windows[0]["pools"]["source"]["requests"] > 0


def test_a_folder_with_earlier_results_is_refused(server, tmp_path, capsys):
    """이전 PASS 리포트가 남은 폴더에 다시 돌리면, 새 실행이 죽었을 때 옛 판정을 읽게 된다."""
    (tmp_path / "report.json").write_text('{"verdict": "PASS"}')
    assert main(args(server, tmp_path)) == 2
    assert "이미" in capsys.readouterr().err
    assert (tmp_path / "report.json").read_text() == '{"verdict": "PASS"}'
    assert not (tmp_path / "journal.jsonl").exists()


def test_unexpected_crash_exits_two_and_hides_the_message(server, tmp_path, capsys, monkeypatch):
    """예상 못 한 오류의 종료 코드는 '실패 있음'(1)이 아니라 '시험 불가'(2)다. 메시지에는 접속 정보가 섞일 수 있다."""

    def explode(*_args, **_kwargs):
        raise RuntimeError("postgresql://u:topsecret@db/none")

    monkeypatch.setattr("bravo_loadgen.cli.read_journal", explode)
    assert main(args(server, tmp_path)) == 2
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err and "topsecret" not in captured.err + captured.out


def test_url_without_a_scheme_is_a_configuration_error(tmp_path, capsys):
    assert main(args("127.0.0.1:8080", tmp_path)) == 2
    assert "--base-url" in capsys.readouterr().err
    assert not (tmp_path / "journal.jsonl").exists()


def test_malformed_dsn_stops_before_load_and_hides_the_value(server, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LOADGEN_TEST_DSN", "postgresql://chat:Sup3r%Secret@db/none")
    assert main(args(server, tmp_path, "--lock-dsn-env", "LOADGEN_TEST_DSN")) == 2
    captured = capsys.readouterr()
    assert "Sup3r" not in captured.err + captured.out and "LOADGEN_TEST_DSN" in captured.err
    assert not (tmp_path / "journal.jsonl").exists()


def test_dsn_value_passed_as_variable_name_is_not_echoed(server, tmp_path, capsys):
    bad = "postgresql://u:topsecret@db/none"
    assert main(args(server, tmp_path, "--db-dsn-env", bad)) == 2
    captured = capsys.readouterr()
    assert "topsecret" not in captured.err + captured.out


def test_frozen_database_read_gives_up_as_unknown(server, tmp_path, monkeypatch):
    """대조 조회에서 DB가 멈춰도 리포트 없이 영원히 기다리지 않는다."""

    def hang(*_args):
        threading.Event().wait()

    monkeypatch.setattr("bravo_loadgen.cli.fetch_rows", hang)
    monkeypatch.setattr("bravo_loadgen.cli.DB_READ_TIMEOUT_S", 0.1)
    monkeypatch.setenv("LOADGEN_TEST_DSN", "postgresql://u:p@127.0.0.1:1/none")
    assert main(args(server, tmp_path, "--db-dsn-env", "LOADGEN_TEST_DSN")) == 2
    assert json.loads((tmp_path / "report.json").read_text())["reasons"] == ["NOT_RECONCILED"]


def test_probe_phases_follow_the_load(server, tmp_path, monkeypatch):
    """"초당 50건일 때"의 표본은 부하 구간에서만 나온다. 앞뒤 구간과 섞이면 값이 흐려진다."""
    phases = {}

    class FakeProbe:
        key = 1
        connection_errors = 0
        samples = []

        def __init__(self, _dsn):
            self._phase = "baseline"
            phases["start"] = self._phase

        @property
        def phase(self):
            return self._phase

        @phase.setter
        def phase(self, value):
            phases.setdefault("order", []).append(value)
            self._phase = value

        def start(self):
            pass

        def stop(self):
            phases["at_stop"] = self._phase

    monkeypatch.setattr("bravo_loadgen.cli.LockProbe", FakeProbe)
    monkeypatch.setenv("LOADGEN_TEST_DSN", "postgresql://u:p@127.0.0.1:1/none")
    main(args(server, tmp_path, "--lock-dsn-env", "LOADGEN_TEST_DSN", "--lock-baseline", "0.1"))
    assert phases["start"] == "baseline"
    assert phases["order"] == ["load", "drain"] and phases["at_stop"] == "drain"


def test_check_windows_passes_a_real_output_then_catches_tampering(server, tmp_path, capsys):
    main(args(server, tmp_path, "--duration", "2.3"))
    path = tmp_path / "windows.jsonl"
    assert main(["check-windows", str(path), "--window-seconds", "1"]) == 0

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    broken = json.loads(lines[1])
    broken["pools"]["target"]["http_5xx"] = -1
    path.write_text(lines[0] + "\n" + json.dumps(broken) + "\n")
    assert main(["check-windows", str(path), "--window-seconds", "1"]) == 1
    assert "스키마" in capsys.readouterr().out


def test_check_windows_catches_a_gap(tmp_path, capsys):
    example = {
        "kind": "sli_window", "schema_version": "1", "plan_id": "p", "source": "loadgen",
        "received_at": "2026-11-08T05:31:21Z",
        "pools": {
            "source": {"requests": 0, "http_5xx": 0, "integrity_conflict": 0, "p95_ms": None},
            "target": {"requests": 0, "http_5xx": 0, "integrity_conflict": 0, "p95_ms": None},
        },
        "unattributed": {"timeout": 0, "connect": 0, "other": 0},
    }
    a = {**example, "window": {"start": "2026-11-08T05:31:10Z", "end": "2026-11-08T05:31:20Z"}}
    b = {**example, "window": {"start": "2026-11-08T05:31:40Z", "end": "2026-11-08T05:31:50Z"}}
    path = tmp_path / "w.jsonl"
    path.write_text(json.dumps(a) + "\n" + json.dumps(b) + "\n")
    assert main(["check-windows", str(path)]) == 1
    assert "GAP" in capsys.readouterr().out


def test_check_windows_on_an_empty_file_is_not_a_pass(tmp_path):
    path = tmp_path / "w.jsonl"
    path.write_text("")
    assert main(["check-windows", str(path)]) == 2


def test_window_url_receives_the_same_windows_as_the_file(server, tmp_path):
    main(args(server, tmp_path, "--window-url", f"{server.url}/windows"))
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["windows"]["delivery_failed"] == 0
    assert server.windows == [
        json.loads(line) for line in (tmp_path / "windows.jsonl").read_text().splitlines()
    ]
    assert len(server.windows) == 1


def test_window_receiver_rejection_is_counted_and_the_load_finishes(server, tmp_path):
    main(args(server, tmp_path, "--window-url", f"{server.url}/nope"))
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["windows"]["delivery_failed"] == 1
    assert "WINDOW_DELIVERY_FAILED" in report["warnings"]
    assert report["requests"]["write"]["attempts"] == 12


def test_missing_dsn_variable_stops_before_sending_load(server, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("LOADGEN_TEST_DSN", raising=False)
    assert main(args(server, tmp_path, "--db-dsn-env", "LOADGEN_TEST_DSN")) == 2
    assert not (tmp_path / "journal.jsonl").exists()
    assert "LOADGEN_TEST_DSN" in capsys.readouterr().err


def test_credentials_in_the_target_url_are_rejected(tmp_path, capsys):
    bad = args("http://user:secret@127.0.0.1:1", tmp_path)
    assert main(bad) == 2
    captured = capsys.readouterr()
    assert "secret" not in captured.err + captured.out
    assert not (tmp_path / "report.json").exists()


def test_invalid_configuration_exits_two(server, tmp_path, capsys):
    bad = args(server, tmp_path, "--run-id", "Not Valid")
    assert main(bad) == 2
    assert "run_id" in capsys.readouterr().err


def test_same_source_and_target_env_is_rejected(server, tmp_path):
    bad = args(server, tmp_path)
    bad[bad.index("--target-env") + 1] = "aws"
    assert main(bad) == 2


def test_reconcile_of_an_unfinished_run_is_not_a_pass(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LOADGEN_TEST_DSN", "postgresql://u:p@127.0.0.1:1/none")
    assert main(["reconcile", "--out", str(tmp_path), "--db-dsn-env", "LOADGEN_TEST_DSN"]) == 2
    assert "기록이 없다" in capsys.readouterr().err

    (tmp_path / "journal.jsonl").write_text("")
    (tmp_path / "run-state.json").write_text("{}")
    assert main(["reconcile", "--out", str(tmp_path), "--db-dsn-env", "LOADGEN_TEST_DSN"]) == 2


def test_too_low_file_limit_stops_before_sending_load(server, tmp_path, capsys, monkeypatch):
    """연결이 모자라 난 실패를 서버 장애로 잘못 세면 안 된다."""
    monkeypatch.setattr("resource.getrlimit", lambda _: (256, 1024))
    bad = args(server, tmp_path, "--write-rps", "50", "--read-rps", "150")
    assert main(bad) == 2
    assert "상한" in capsys.readouterr().err
    assert not (tmp_path / "journal.jsonl").exists()


def test_open_file_budget_covers_retries_and_reads():
    """쓰기는 재시도 한도 동안, 읽기는 시간 초과 동안 열려 있을 수 있다."""
    assert open_files_needed(write_rps=50, read_rps=150, retry_budget_s=60, attempt_timeout_s=15) >= 5250


def test_unreadable_database_is_unknown_and_hides_the_dsn(server, tmp_path, capsys, monkeypatch):
    dsn = "postgresql://u:topsecret@127.0.0.1:1/none?connect_timeout=1"
    monkeypatch.setenv("LOADGEN_TEST_DSN", dsn)
    assert main(args(server, tmp_path, "--db-dsn-env", "LOADGEN_TEST_DSN")) == 2
    captured = capsys.readouterr()
    assert "topsecret" not in captured.err + captured.out
    text = "".join(p.read_text() for p in tmp_path.iterdir())
    assert "topsecret" not in text
    assert json.loads((tmp_path / "report.json").read_text())["reasons"] == ["NOT_RECONCILED"]
