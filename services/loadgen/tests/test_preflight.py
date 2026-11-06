"""사전 점검: 실패하면 부하를 한 건도 보내지 않고, 비밀을 어디에도 남기지 않는지 실제 소켓으로 본다."""

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from bravo_loadgen import cli
from bravo_loadgen.cli import main

TOKEN = "t18-test-window-token"
DSN_SECRET = "dsn-secret-value"


def info_body(write_blocked):
    """demo-chat `/api/info`의 본문 모양. 쓰기 차단 상태는 Pod마다 따로다."""
    return {"env": "aws", "write_blocked": write_blocked, "attachment_upload_enabled": True}


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def date_time_string(self, timestamp=None):
        return self.server.date or super().date_time_string(timestamp)

    def _send(self, status, body, extra=(), env_headers=None):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        for value in self.server.env_headers if env_headers is None else env_headers:
            self.send_header("X-Bravo-Env", value)
        for name, value in (*self.server.extra_headers, *extra):
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.server.auth_on_get.append(self.headers.get("Authorization"))
        if self.path == "/readyz":
            return self._send(200, {"status": "ok"})
        if self.path == "/moved-ok":
            self.server.followed_redirect = True
            return self._send(200, {"ok": True})
        if self.path == "/api/health/db":
            self.server.health_calls += 1
            time.sleep(self.server.health_delay)
            return self._send(*self.server.health)
        if self.path == "/api/info":
            # Edge가 요청마다 환경 · Pod를 고르는 것처럼 정한 답을 돌아가며 준다.
            env, status, body = self.server.info[self.server.info_calls % len(self.server.info)]
            self.server.info_calls += 1
            time.sleep(self.server.info_delay)
            return self._send(status, body, env_headers=None if env is None else [env])
        self.server.load_requests += 1
        return self._send(200, [])

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/windows":
            self.server.window_posts.append((self.headers.get("Authorization"), body))
            time.sleep(self.server.window_delay)
            if json.loads(body) == {}:
                return self._send(*self.server.window_probe)
            self.server.stored_windows += 1
            return self._send(201, {})
        self.server.load_requests += 1
        self.server.count += 1
        return self._send(201, {"id": self.server.count})


def _start_server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.health = (200, {"ok": True})
    httpd.env_headers = ["aws"]
    httpd.extra_headers = []
    httpd.date = None
    httpd.health_delay = 0
    httpd.health_calls = 0
    httpd.info = [(None, 200, info_body(False))]
    httpd.info_calls = 0
    httpd.info_delay = 0
    httpd.window_delay = 0
    httpd.window_probe = (422, {"detail": "invalid_window"})
    httpd.window_posts = []
    httpd.stored_windows = 0
    httpd.load_requests = 0
    httpd.auth_on_get = []
    httpd.count = 0
    httpd.followed_redirect = False
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}"
    return httpd


@pytest.fixture
def target():
    httpd = _start_server()
    yield httpd
    httpd.shutdown()


@pytest.fixture
def receiver():
    httpd = _start_server()
    yield httpd
    httpd.shutdown()


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setenv("BRAVO_WINDOW_TOKEN", TOKEN)
    return TOKEN


def closed_port_url():
    probe = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port = probe.server_address[1]
    probe.server_close()
    return f"http://127.0.0.1:{port}"


def command(name, base_url, out, *extra, path="local-mock-edge"):
    return [
        name, "--base-url", base_url, "--plan-id", "plan-demo",
        "--source-env", "aws", "--target-env", "onprem", "--measurement-path", path,
        "--duration", "1.2", "--write-rps", "10", "--read-rps", "10", "--window-seconds", "1",
        "--out", str(out), "--run-id", "t18-joint", "--attempt-timeout", "2", *extra,
    ]


def preflight(target, out, *extra, path="local-mock-edge"):
    return main(command("preflight", target.url, out, *extra, path=path))


def test_healthy_target_passes_without_sending_load(target, tmp_path, capsys):
    assert preflight(target, tmp_path / "out") == 0
    assert target.load_requests == 0 and target.health_calls == 5
    assert "사전 점검 통과" in capsys.readouterr().out
    assert not (tmp_path / "out").exists()


def test_broken_database_fails_even_when_readyz_is_fine(target, tmp_path, capsys):
    """/readyz는 앱이 떠 있다는 뜻이다. DB까지 닿는지는 /api/health/db로만 본다."""
    target.health = (503, {"detail": "database unavailable"})
    assert preflight(target, tmp_path / "out") == 2
    assert "/api/health/db" in capsys.readouterr().out
    assert target.load_requests == 0


def test_non_200_with_an_app_like_body_is_not_healthy(target, tmp_path):
    target.health = (503, {"ok": True})
    assert preflight(target, tmp_path / "out") == 2


@pytest.mark.parametrize("body", [{"ok": False}, {"status": "ok"}, ["ok"]])
def test_200_with_an_unexpected_body_is_not_healthy(target, tmp_path, body):
    """Edge의 안내 페이지처럼 200이지만 앱의 답이 아닌 경우를 막는다."""
    target.health = (200, body)
    assert preflight(target, tmp_path / "out") == 2


def test_redirect_is_not_followed_and_fails(target, tmp_path):
    target.health = (302, {})
    target.extra_headers = [("Location", "/moved-ok")]
    assert preflight(target, tmp_path / "out") == 2
    assert target.followed_redirect is False


def test_connection_failure_fails_and_shows_only_the_kind(tmp_path, capsys):
    url = closed_port_url()
    assert main(command("preflight", url, tmp_path / "out")) == 2
    out = capsys.readouterr().out
    assert "ConnectError" in out and url not in out


def test_slow_health_response_fails(target, tmp_path, monkeypatch):
    monkeypatch.setattr("bravo_loadgen.preflight.HEALTH_TIMEOUT_S", 0.1)
    target.health_delay = 0.5
    assert preflight(target, tmp_path / "out") == 2


@pytest.mark.parametrize(
    "headers", [[], ["staging"], ["aws", "aws"]], ids=["없음", "등록되지 않은 값", "Edge가 덧붙임"]
)
def test_env_header_problem_fails(target, tmp_path, capsys, headers):
    target.env_headers = headers
    assert preflight(target, tmp_path / "out") == 2
    assert "X-Bravo-Env" in capsys.readouterr().out
    assert target.load_requests == 0


def test_seen_env_values_are_shown_so_a_wrong_starting_weight_is_visible(target, tmp_path, capsys):
    target.env_headers = ["onprem"]
    assert preflight(target, tmp_path / "out") == 0
    assert "onprem 5" in capsys.readouterr().out


def test_open_writes_pass_and_the_token_is_not_sent(target, tmp_path, capsys):
    assert preflight(target, tmp_path / "out") == 0
    assert target.info_calls == 5
    assert "aws 열림 5" in capsys.readouterr().out
    assert set(target.auth_on_get) == {None}


def test_closed_target_writes_fail_and_name_the_open_command(target, tmp_path, capsys):
    """목적지 앱은 닫힌 채 뜨고 재시작하면 다시 닫힌다. 그대로 재면 목적지로 간 쓰기가 전부 503이다."""
    target.info = [("aws", 200, info_body(False)), ("onprem", 200, info_body(True))]
    assert preflight(target, tmp_path / "out") == 2
    out = capsys.readouterr().out
    assert "onprem 닫힘 2" in out and "write_switch.py open" in out
    assert target.load_requests == 0


def test_closed_source_writes_fail_without_the_target_command(target, tmp_path, capsys):
    target.info = [("aws", 200, info_body(True))]
    assert preflight(target, tmp_path / "out") == 2
    out = capsys.readouterr().out
    assert "aws 닫힘 5" in out and "write_switch.py open" not in out


def test_one_closed_pod_among_open_ones_fails(target, tmp_path, capsys):
    """스위치는 Pod마다 따로라 한 Pod만 다시 떠도 그 Pod로 간 쓰기는 503이다."""
    target.info = [("onprem", 200, info_body(False))] * 4 + [("onprem", 200, info_body(True))]
    assert preflight(target, tmp_path / "out") == 2
    assert "onprem 열림 4, 닫힘 1" in capsys.readouterr().out


@pytest.mark.parametrize(
    "answer",
    [
        (200, {"env": "aws"}),
        (200, info_body("false")),
        (404, {"detail": "Not Found"}),
        (503, info_body(False)),
        (200, ["aws"]),
    ],
    ids=["필드 없음", "문자열 값", "404", "본문이 열림인 503", "목록"],
)
def test_unreadable_write_state_is_unconfirmed_not_failed(target, tmp_path, capsys, answer):
    target.info = [("aws", *answer)]
    assert preflight(target, tmp_path / "out") == 0
    assert "aws 확인 못 함 5" in capsys.readouterr().out


def test_target_unseen_at_weight_zero_is_unconfirmed_and_points_to_status(target, tmp_path, capsys):
    """가중치 0이면 Edge가 목적지로 보내지 않아 목적지 스위치를 이 경로로는 볼 수 없다."""
    assert preflight(target, tmp_path / "out") == 0
    out = capsys.readouterr().out
    assert "onprem 응답 없어 확인 못 함" in out and "write_switch.py status" in out


def test_info_timeout_is_unconfirmed_and_shows_only_the_kind(target, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("bravo_loadgen.preflight.HEALTH_TIMEOUT_S", 0.1)
    target.info_delay = 0.5
    assert preflight(target, tmp_path / "out") == 0
    assert "/api/info 접속 실패: ReadTimeout" in capsys.readouterr().out


def test_no_write_load_does_not_look_at_the_switch(target, tmp_path, capsys):
    target.info = [("onprem", 200, info_body(True))]
    arguments = command("preflight", target.url, tmp_path / "out")
    arguments[arguments.index("--write-rps") + 1] = "0"
    assert main(arguments) == 0
    assert target.info_calls == 0
    assert "쓰기 부하가 없어" in capsys.readouterr().out


def test_edge_path_needs_a_generator_declaration(target, tmp_path, capsys):
    assert preflight(target, tmp_path / "out", path="edge") == 2
    assert "--generator" in capsys.readouterr().err
    assert target.health_calls == 0


def test_edge_path_without_a_cloudflare_response_fails(target, tmp_path, capsys):
    """사람이 edge라고 적어도 응답이 Cloudflare를 지나지 않았으면 실측으로 표시하지 않는다."""
    assert preflight(target, tmp_path / "out", "--generator", "laptop", path="edge") == 2
    assert "CF-RAY" in capsys.readouterr().out


def test_edge_path_with_a_cloudflare_response_passes(target, tmp_path):
    target.extra_headers = [("CF-RAY", "local-test")]
    assert preflight(target, tmp_path / "out", "--generator", "laptop", path="edge") == 0


def test_ec2_claim_without_the_host_marker_is_refused(target, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "EC2_MARKER", tmp_path / "missing")
    assert preflight(target, tmp_path / "out", "--generator", "loadgen-ec2") == 2
    assert "loadgen-ec2" in capsys.readouterr().err
    assert target.health_calls == 0


def test_ec2_claim_with_the_host_marker_passes(target, tmp_path, monkeypatch):
    marker = tmp_path / "ready"
    marker.touch()
    monkeypatch.setattr(cli, "EC2_MARKER", marker)
    assert preflight(target, tmp_path / "out", "--generator", "loadgen-ec2") == 0


def test_laptop_windows_are_not_sent_to_the_judgement_path(target, tmp_path, token, capsys):
    target.extra_headers = [("CF-RAY", "local-test")]
    url = f"{target.url}/windows"
    code = preflight(target, tmp_path / "out", "--generator", "laptop", "--window-url", url, path="edge")
    assert code == 2
    assert "--window-url" in capsys.readouterr().err
    assert target.window_posts == []


def test_window_probe_proves_the_path_without_storing_a_window(target, tmp_path, token):
    assert preflight(target, tmp_path / "out", "--window-url", f"{target.url}/windows") == 0
    assert target.window_posts == [(f"Bearer {TOKEN}", b"{}")]
    assert target.stored_windows == 0
    # 토큰은 창 수신에만 간다.
    assert set(target.auth_on_get) == {None}


@pytest.mark.parametrize(
    ("response", "word"),
    [
        ((401, {"detail": "unauthorized"}), "토큰"),
        ((403, {"detail": "forbidden"}), "계획"),
        ((201, {}), "수신 경로가 아니다"),
        ((503, {"detail": "window_store_unavailable"}), "503"),
        ((404, {"detail": "Not Found"}), "404"),
        ((422, {"detail": "something_else"}), "422"),
    ],
)
def test_window_receiver_problem_fails_before_load(target, tmp_path, token, capsys, response, word):
    target.window_probe = response
    assert preflight(target, tmp_path / "out", "--window-url", f"{target.url}/windows") == 2
    assert word in capsys.readouterr().out
    assert target.load_requests == 0


def test_unreachable_window_receiver_fails(target, tmp_path, token, capsys):
    assert preflight(target, tmp_path / "out", "--window-url", f"{closed_port_url()}/windows") == 2
    assert "ConnectError" in capsys.readouterr().out


def test_window_receiver_slower_than_the_send_limit_fails(target, tmp_path, token, monkeypatch, capsys):
    """창 전송 제한을 못 맞추는 경로는 본 측정에서 창을 전부 잃는다."""
    monkeypatch.setattr(cli, "WINDOW_POST_TIMEOUT_S", 0.1)
    target.window_delay = 0.5
    assert preflight(target, tmp_path / "out", "--window-url", f"{target.url}/windows") == 2
    assert "ReadTimeout" in capsys.readouterr().out


def test_missing_window_token_stops_before_any_request(target, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BRAVO_WINDOW_TOKEN", raising=False)
    assert preflight(target, tmp_path / "out", "--window-url", f"{target.url}/windows") == 2
    assert "BRAVO_WINDOW_TOKEN" in capsys.readouterr().err
    assert target.health_calls == 0


def test_missing_database_variable_stops_before_any_request(target, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("LOADGEN_DB_DSN", raising=False)
    assert preflight(target, tmp_path / "out", "--db-dsn-env", "LOADGEN_DB_DSN") == 2
    assert "LOADGEN_DB_DSN" in capsys.readouterr().err
    assert target.health_calls == 0


def test_unreachable_database_fails_and_hides_the_connection_string(target, tmp_path, capsys, monkeypatch):
    port = closed_port_url().rsplit(":", 1)[1]
    monkeypatch.setenv("LOADGEN_DB_DSN", f"postgresql://reader:{DSN_SECRET}@127.0.0.1:{port}/chat")
    assert preflight(target, tmp_path / "out", "--db-dsn-env", "LOADGEN_DB_DSN") == 2
    captured = capsys.readouterr()
    assert "OperationalError" in captured.out
    assert DSN_SECRET not in captured.out + captured.err


def test_no_database_is_allowed_but_says_reconcile_comes_later(target, tmp_path, capsys):
    assert preflight(target, tmp_path / "out") == 0
    assert "reconcile" in capsys.readouterr().out


def test_clock_far_from_the_server_fails(target, tmp_path, capsys):
    """시계가 어긋나면 창이 CLOCK_SKEW로 전부 UNKNOWN이 된다."""
    target.date = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert preflight(target, tmp_path / "out") == 2
    assert "시계" in capsys.readouterr().out


def test_receiver_clock_is_the_one_compared(target, receiver, tmp_path, token, capsys):
    """판정은 플랫폼 시계로 한다. 대상의 시계가 맞아도 창 수신과 어긋나면 막는다."""
    receiver.date = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert preflight(target, tmp_path / "out", "--window-url", f"{receiver.url}/windows") == 2
    assert "시계" in capsys.readouterr().out


def test_invalid_run_id_fails_before_any_request(target, tmp_path, capsys):
    arguments = command("preflight", target.url, tmp_path / "out")
    arguments[arguments.index("t18-joint")] = "T18_NOT_VALID"
    assert main(arguments) == 2
    assert target.health_calls == 0


@pytest.mark.parametrize("name", ["preflight", "measure"])
def test_run_id_is_required_so_both_records_can_be_matched(target, tmp_path, capsys, name):
    arguments = command(name, target.url, tmp_path / "out")
    index = arguments.index("--run-id")
    del arguments[index : index + 2]
    assert main(arguments) == 2
    assert "--run-id" in capsys.readouterr().err
    assert target.health_calls == 0


def test_measure_stops_when_preflight_fails(target, tmp_path):
    target.health = (503, {})
    assert main(command("measure", target.url, tmp_path / "out")) == 2
    assert target.load_requests == 0
    assert not (tmp_path / "out").exists()


def test_measure_runs_the_load_and_keeps_the_preflight_with_the_report(target, tmp_path, token):
    out = tmp_path / "out"
    code = main(command("measure", target.url, out, "--generator", "laptop", "--window-url", f"{target.url}/windows"))
    assert code == 2  # DB가 없으니 NOT_RECONCILED다. 통과가 아니다.
    assert target.load_requests > 0 and target.stored_windows > 0
    saved = json.loads((out / "preflight.json").read_text())
    report = json.loads((out / "report.json").read_text())
    assert saved["passed"] is True and saved["env_values"] == {"aws": 5}
    assert "쓰기 차단" in [check["name"] for check in saved["checks"]]
    assert report["run_id"] == "t18-joint"
    assert report["measurement"]["generator"] == "laptop"
    assert report["measurement"]["preflight"]["health_rtt_ms"]["samples"] == 5
    assert report["sli_usable_for_judgement"] is False


def test_secrets_are_left_nowhere_on_pass_or_failure(target, tmp_path, token, capfd, monkeypatch):
    port = closed_port_url().rsplit(":", 1)[1]
    url = f"{target.url}/windows"
    out = tmp_path / "out"
    monkeypatch.setenv("LOADGEN_DB_DSN", f"postgresql://reader:{DSN_SECRET}@127.0.0.1:{port}/chat")
    target.window_probe = (401, {"detail": "unauthorized"})
    assert preflight(target, tmp_path / "failed", "--window-url", url, "--db-dsn-env", "LOADGEN_DB_DSN") == 2
    target.window_probe = (422, {"detail": "invalid_window"})
    monkeypatch.delenv("LOADGEN_DB_DSN")
    main(command("measure", target.url, out, "--generator", "laptop", "--window-url", url))
    captured = capfd.readouterr()
    written = "".join(p.read_text() for p in out.iterdir())
    for secret in (TOKEN, DSN_SECRET):
        assert secret not in captured.out + captured.err + written
