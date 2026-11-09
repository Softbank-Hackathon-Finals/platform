import pytest

from rehearsal_runner import credential_job_probe


@pytest.mark.parametrize("mode,result,expected", [
    ("secret-get", "no", 0), ("secret-get", "yes", 0), ("app-db", "yes", 0),
    ("app-db", "unknown", 2),
])
def test_job_emits_only_observation(monkeypatch, capsys, mode, result, expected):
    monkeypatch.setattr(credential_job_probe, "observe_secret_get", lambda *a, **k: result)
    monkeypatch.setattr(credential_job_probe, "observe_app_database", lambda *a, **k: result)
    args = (["secret-get", "--namespace", "rehearsal-x", "--name", "app"] if mode == "secret-get" else
            ["app-db", "--namespace", "rehearsal-x", "--service", "candidate", "--port", "8000"])
    assert credential_job_probe.main(args) == expected
    assert capsys.readouterr().out == '{"observation":"' + result + '"}\n'


def test_unexpected_probe_error_is_not_leaked(monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError("sensitive-value")
    monkeypatch.setattr(credential_job_probe, "observe_secret_get", fail)
    assert credential_job_probe.main(["secret-get", "--namespace", "rehearsal-x", "--name", "app"]) == 2
    captured = capsys.readouterr()
    assert captured.out == '{"observation":"unknown"}\n'
    assert "sensitive-value" not in captured.out + captured.err
