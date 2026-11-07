"""명령줄: 실제 소켓으로 끝까지 돌려 산출물과 종료 코드를 본다. DB가 필요한 시험은 test_db.py."""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from test_signin import jwt

from bravo_loadgen.cli import DEFAULT_WINDOW_SECONDS, main, open_files_needed

PROFILE = json.loads(
    (Path(__file__).resolve().parents[3] / "docs/observability/threshold-profiles.json").read_text()
)["profiles"][0]
WINDOW_TOKEN = "t18-test-window-token"
PASSWORD = "do-not-print-this-password"
CLIENT_SECRET = "do-not-print-this-secret"
ID_TOKEN = jwt(preferred_username="loadgen")


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
        if self.path == "/realms/bravo/.well-known/openid-configuration":
            issuer = f"{self.server.url}/realms/bravo"
            return self._send(200, {"issuer": issuer, "token_endpoint": f"{issuer}/token"})
        self.server.load_auth.append(self.headers.get("Authorization"))
        self.server.read_auth.append(self.headers.get("Authorization"))
        self._send(200, [])

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path == "/windows":
            self.server.window_auth.append(self.headers.get("Authorization"))
            self.server.windows.append(json.loads(body))
            return self._send(202, {})
        if self.path == "/denied":
            return self._send(401, {})
        if self.path == "/moved":
            self.send_response(307)
            self.send_header("Location", "/followed")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None
        if self.path == "/realms/bravo/token":
            self.server.grants.append(body.decode())
            return self._send(200, {"id_token": ID_TOKEN})
        if self.path == "/followed":
            self.server.followed.append(self.headers.get("Authorization"))
            return self._send(202, {})
        if self.path != "/api/messages":
            return self._send(404, {})
        self.server.load_auth.append(self.headers.get("Authorization"))
        self.server.write_auth.append(self.headers.get("Authorization"))
        self.server.authors.append(json.loads(body)["author"])
        self.server.count += 1
        self._send(201, {"id": self.server.count})


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.count = 0
    httpd.windows = []
    httpd.window_auth = []
    httpd.load_auth = []
    httpd.read_auth = []
    httpd.write_auth = []
    httpd.authors = []
    httpd.grants = []
    httpd.followed = []
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield httpd
    httpd.shutdown()


@pytest.fixture
def window_token(monkeypatch):
    monkeypatch.setenv("BRAVO_WINDOW_TOKEN", WINDOW_TOKEN)
    return WINDOW_TOKEN


def args(server, out, *extra):
    return [
        "run", "--base-url", getattr(server, "url", server), "--plan-id", "plan-demo",
        "--source-env", "aws", "--target-env", "onprem",
        "--duration", "1.2", "--write-rps", "10", "--read-rps", "10", "--window-seconds", "1",
        "--measurement-path", "local-direct", "--out", str(out), "--run-id", "t18-cli",
        "--attempt-timeout", "2", *extra,
    ]


def sign_in_args(server):
    return ["--auth-issuer", f"{server.url}/realms/bravo", "--auth-client-id", "demo-chat-loadgen",
            "--auth-username", "loadgen"]


@pytest.fixture
def sign_in_secrets(monkeypatch):
    monkeypatch.setenv("BRAVO_LOADGEN_PASSWORD", PASSWORD)
    monkeypatch.setenv("BRAVO_LOADGEN_CLIENT_SECRET", CLIENT_SECRET)


def test_signed_in_run_sends_the_token_on_writes_only(server, tmp_path, capsys, sign_in_secrets):
    """로그인을 켠 앱(T47): 쓰기에 ID 토큰, 작성자는 토큰 이름. 읽기는 로그인 없이 본다."""
    assert main(args(server, tmp_path, *sign_in_args(server))) == 2  # DB가 없어 NOT_RECONCILED
    assert server.write_auth and set(server.write_auth) == {f"Bearer {ID_TOKEN}"}
    assert server.read_auth and set(server.read_auth) == {None}
    assert set(server.authors) == {"loadgen"}
    assert len(server.grants) == 1  # 첫 토큰은 부하 전에 한 번
    journal = [json.loads(line) for line in (tmp_path / "journal.jsonl").read_text().splitlines()]
    assert journal[0]["author"] == "loadgen"
    sign_in = {"kind": "oidc", "refreshes": 0, "refresh_failures": 0}
    assert json.loads((tmp_path / "run-state.json").read_text())["sign_in"] == sign_in
    assert json.loads((tmp_path / "report.json").read_text())["measurement"]["sign_in"] == sign_in

    # 비밀번호 · 클라이언트 비밀 · 토큰은 산출물과 화면 어디에도 없다.
    captured = capsys.readouterr()
    written = "".join(path.read_text() for path in tmp_path.iterdir()) + captured.out + captured.err
    for secret in (PASSWORD, CLIENT_SECRET, ID_TOKEN):
        assert secret not in written


def test_sign_in_is_off_by_default(server, tmp_path):
    assert main(args(server, tmp_path)) == 2
    assert server.grants == [] and set(server.write_auth) == {None}
    assert json.loads((tmp_path / "run-state.json").read_text())["sign_in"] is None


@pytest.mark.parametrize("drop", ["--auth-client-id", "--auth-username"])
def test_partial_sign_in_settings_stop_before_any_request(server, tmp_path, capsys, sign_in_secrets, drop):
    extra = sign_in_args(server)
    index = extra.index(drop)
    del extra[index:index + 2]
    assert main(args(server, tmp_path, *extra)) == 2
    assert "--auth-" in capsys.readouterr().err
    assert server.grants == [] and server.count == 0


def test_missing_password_stops_before_any_request(server, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("BRAVO_LOADGEN_CLIENT_SECRET", CLIENT_SECRET)
    monkeypatch.delenv("BRAVO_LOADGEN_PASSWORD", raising=False)
    assert main(args(server, tmp_path, *sign_in_args(server))) == 2
    assert "BRAVO_LOADGEN_PASSWORD" in capsys.readouterr().err
    assert server.grants == [] and server.count == 0
    assert not (tmp_path / "journal.jsonl").exists()


def test_refused_sign_in_stops_before_load(server, tmp_path, capsys, sign_in_secrets):
    extra = sign_in_args(server)
    extra[1] = f"{server.url}/realms/missing"  # 발급자 문서 대신 메시지 목록이 온다
    assert main(args(server, tmp_path, *extra)) == 2
    assert "발급자 문서" in capsys.readouterr().err
    assert server.count == 0 and not (tmp_path / "journal.jsonl").exists()


def test_window_seconds_default_matches_threshold_profile():
    assert DEFAULT_WINDOW_SECONDS == PROFILE["window_seconds"]


@pytest.mark.parametrize("url", ["not a url", "http://", "http://host:notaport/"])
def test_invalid_window_url_stops_before_sending_load(server, tmp_path, capsys, window_token, url):
    assert main(args(server, tmp_path, "--window-url", url)) == 2
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


def test_window_url_receives_the_same_windows_as_the_file(server, tmp_path, window_token):
    main(args(server, tmp_path, "--window-url", f"{server.url}/windows"))
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["windows"]["delivery_failed"] == 0
    assert server.windows == [
        json.loads(line) for line in (tmp_path / "windows.jsonl").read_text().splitlines()
    ]
    assert len(server.windows) == 1


def test_window_receiver_rejection_is_counted_and_the_load_finishes(server, tmp_path, window_token):
    main(args(server, tmp_path, "--window-url", f"{server.url}/nope"))
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["windows"]["delivery_failed"] == 1
    assert "WINDOW_DELIVERY_FAILED" in report["warnings"]
    assert report["requests"]["write"]["attempts"] == 12


# 시험에 쓸 base64도 그대로 지나가야 한다. `openssl rand -base64`로 만든 토큰에 +, /, =가 들어간다.
@pytest.mark.parametrize("token", [WINDOW_TOKEN, "aB3+/-._~=="])
def test_window_post_carries_the_token_and_load_requests_do_not(server, tmp_path, monkeypatch, token):
    """수신 경로의 토큰이 평가 대상 앱으로 새면 안 된다."""
    monkeypatch.setenv("BRAVO_WINDOW_TOKEN", token)
    main(args(server, tmp_path, "--window-url", f"{server.url}/windows"))
    assert server.window_auth == [f"Bearer {token}"]
    assert server.load_auth and set(server.load_auth) == {None}


@pytest.mark.parametrize("value", [None, ""])
def test_window_url_without_a_token_stops_before_sending_load(server, tmp_path, capsys, monkeypatch, value):
    """127.0.0.1도 예외가 아니다. 포트 포워딩이면 그 끝이 실제 수신 경로일 수 있다."""
    monkeypatch.delenv("BRAVO_WINDOW_TOKEN", raising=False)
    if value is not None:
        monkeypatch.setenv("BRAVO_WINDOW_TOKEN", value)
    assert main(args(server, tmp_path, "--window-url", f"{server.url}/windows")) == 2
    assert "BRAVO_WINDOW_TOKEN" in capsys.readouterr().err
    assert not (tmp_path / "journal.jsonl").exists()
    assert server.load_auth == []


def test_token_is_not_needed_without_a_window_url(server, tmp_path, monkeypatch):
    monkeypatch.delenv("BRAVO_WINDOW_TOKEN", raising=False)
    main(args(server, tmp_path))
    assert (tmp_path / "report.json").is_file()


@pytest.mark.parametrize("value", ["bad\ntoken", "token\n", "bad token", "한글토큰"])
def test_malformed_token_stops_before_load_and_hides_the_value(server, tmp_path, capsys, monkeypatch, value):
    """헤더에 못 넣는 값은 부하 중간에 값째 담긴 오류로 터진다. 시작 전에 막는다."""
    monkeypatch.setenv("BRAVO_WINDOW_TOKEN", value)
    assert main(args(server, tmp_path, "--window-url", f"{server.url}/windows")) == 2
    captured = capsys.readouterr()
    assert "BRAVO_WINDOW_TOKEN" in captured.err
    assert value not in captured.out + captured.err
    assert not (tmp_path / "journal.jsonl").exists()


@pytest.mark.parametrize("path", ["/windows", "/denied", "/moved"])
def test_token_is_left_in_no_file_output_or_log(server, tmp_path, capsys, caplog, window_token, path):
    caplog.set_level(logging.DEBUG)
    main(args(server, tmp_path, "--window-url", f"{server.url}{path}"))
    captured = capsys.readouterr()
    files = {p.name: p.read_text() for p in tmp_path.iterdir()}
    assert {"journal.jsonl", "windows.jsonl", "run-state.json", "report.json", "report.md"} <= set(files)
    assert files["windows.jsonl"] and captured.out
    assert "HTTP Request: POST" in caplog.text  # 로그를 실제로 모았는지 확인한다.
    assert window_token not in "".join(files.values()) + captured.out + captured.err + caplog.text


def test_token_is_left_nowhere_when_the_receiver_is_down(tmp_path, server, capsys, caplog, window_token):
    caplog.set_level(logging.DEBUG)
    main(args(server, tmp_path, "--window-url", "http://127.0.0.1:1/windows"))
    captured = capsys.readouterr()
    assert json.loads((tmp_path / "report.json").read_text())["windows"]["delivery_failed"] == 1
    files = "".join(p.read_text() for p in tmp_path.iterdir())
    assert window_token not in files + captured.out + captured.err + caplog.text


def test_crash_while_delivering_a_window_hides_the_token(server, tmp_path, capsys, monkeypatch, window_token):
    async def explode(*_args, **_kwargs):
        raise RuntimeError(f"Authorization: Bearer {window_token}")

    monkeypatch.setattr("httpx.AsyncClient.post", explode)
    assert main(args(server, tmp_path, "--window-url", f"{server.url}/windows")) == 2
    captured = capsys.readouterr()
    assert "RuntimeError" in captured.err
    assert window_token not in captured.out + captured.err


def test_redirect_from_the_receiver_is_a_failure_and_the_token_does_not_follow(server, tmp_path, window_token):
    main(args(server, tmp_path, "--window-url", f"{server.url}/moved"))
    report = json.loads((tmp_path / "report.json").read_text())
    assert report["windows"]["delivery_failed"] == 1
    assert server.followed == []


def test_credentials_in_the_window_url_are_rejected(server, tmp_path, capsys, window_token):
    """주소의 자격 증명은 프로세스 목록에 보이고, httpx가 Bearer 헤더를 Basic으로 덮어쓴다."""
    bad = args(server, tmp_path, "--window-url", "http://user:topsecret@127.0.0.1:1/windows")
    assert main(bad) == 2
    captured = capsys.readouterr()
    assert "--window-url" in captured.err
    assert "topsecret" not in captured.out + captured.err
    assert not (tmp_path / "journal.jsonl").exists()


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
