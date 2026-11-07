"""AUD-01.1 from the CNPG primary's own log through the Kubernetes API (pod_log_audit)."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from bravo_controls.audit_probes import AuditSearchProbe, CanaryReceipt
from bravo_controls.pod_log_audit import PodLogAuditSearch
from bravo_controls.runner import Environment, RunContext
from bravo_controls.runner import TestSpec as Spec

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
CONTEXT = RunContext("run-1", "demo-chat", Environment("env-target", "target", "onprem:k8s/target-dev"))
SPEC = Spec("AUD-01.1", "AUD-01", "1", "present", "not_applicable")
MARKER = "bravo_audit_" + "a" * 32
BINDING = "kubernetes-pod-log:onprem:k8s/target-dev:target-dev/target-db-1/postgres:uid-1"


def receipt(started=T0):
    return CanaryReceipt(CONTEXT, "AUD-01.1", MARKER, "postgres", "chat", "target-dev", "public", started,
                         started + timedelta(seconds=1), started + timedelta(minutes=3), frozenset({"login"}))


def stamp(at):
    return at.strftime("%Y-%m-%dT%H:%M:%S.%f") + "123Z"  # kubelet: nanoseconds


def login_line(at, marker=MARKER, user="postgres"):
    record = {"user_name": user, "database_name": "chat", "error_severity": "LOG", "application_name": None,
              "message": f"connection authorized: user={user} database=chat application_name={marker}"}
    return f"{stamp(at)} " + json.dumps({"level": "info", "logger": "postgres", "msg": "record",
                                         "logging_pod": "target-db-1", "record": record})


class Cluster:
    """GET /api/v1/... for one pod and its container log."""

    def __init__(self, lines=(), started=T0 - timedelta(hours=1), restarts=2):
        self.lines, self.started, self.restarts, self.paths = list(lines), started, restarts, []
        self.after_log = None

    def pod(self):
        return {"metadata": {"uid": "uid-1"}, "status": {"containerStatuses": [
            {"name": "postgres", "restartCount": self.restarts,
             "state": {"running": {"startedAt": self.started.strftime("%Y-%m-%dT%H:%M:%SZ")}}}]}}

    def read(self, path):
        self.paths.append(path)
        if "/log?" in path:
            data = "\n".join(self.lines).encode() + b"\n"
            if self.after_log:
                self.after_log(self)
            return data
        return json.dumps(self.pod()).encode()


def search(cluster, now=T0 + timedelta(seconds=10)):
    return PodLogAuditSearch(cluster.read, namespace="target-dev", pod="target-db-1", container="postgres",
                             source_binding=BINDING, clock=lambda: now)


def test_the_login_line_inside_the_window_is_found_and_the_read_is_bounded():
    cluster = Cluster([login_line(T0 + timedelta(seconds=2)),
                       login_line(T0 + timedelta(seconds=2), marker="bravo_audit_" + "b" * 32),  # another canary
                       login_line(T0 - timedelta(seconds=5))])  # before this canary
    batch = search(cluster)(receipt(), T0 + timedelta(seconds=10))
    assert batch.complete and [e.stream for e in batch.events] == ["target-dev/target-db-1/postgres"]
    assert batch.events[0].timestamp == T0 + timedelta(seconds=2, microseconds=0)
    log = next(p for p in cluster.paths if "/log?" in p)
    assert "container=postgres" in log and "timestamps=true" in log and "sinceTime=2026-11-07T11%3A59%3A59Z" in log
    assert "limitBytes=" in log


@pytest.mark.parametrize("change", [
    lambda c: setattr(c, "started", T0 + timedelta(seconds=1)),  # the container (re)started after the canary
    lambda c: setattr(c, "after_log", lambda c: setattr(c, "restarts", 3)),  # restarted during the read
    lambda c: c.lines.append("x" * (10 * 1024 * 1024)),  # the read reached the byte limit
    lambda c: c.lines.append("not-a-timestamp " + MARKER),  # a marker line we cannot place in time
])
def test_a_read_that_may_have_missed_lines_is_incomplete(change):
    cluster = Cluster([login_line(T0 + timedelta(seconds=2))])
    change(cluster)
    assert search(cluster)(receipt(), T0 + timedelta(seconds=10)).complete is False


def test_through_the_audit_probe_the_login_is_present_and_a_missing_one_is_absent_only_after_the_deadline():
    found = AuditSearchProbe(receipt_provider=lambda *_: receipt(), search=search(Cluster([login_line(T0)])),
                             source_binding=BINDING, streams=("target-dev/target-db-1/postgres",),
                             evidence_sink=lambda _: None, clock=lambda: T0 + timedelta(seconds=10))
    assert found(CONTEXT, SPEC).observed == "present"
    early, late = T0 + timedelta(seconds=10), T0 + timedelta(minutes=4)
    for now, observed in ((early, None), (late, "absent")):
        probe = AuditSearchProbe(receipt_provider=lambda *_: receipt(), search=search(Cluster(), now=now),
                                 source_binding=BINDING, streams=("target-dev/target-db-1/postgres",),
                                 evidence_sink=lambda _: None, clock=lambda now=now: now)
        assert probe(CONTEXT, SPEC).observed == observed


def test_another_users_login_with_the_marker_is_not_the_admin_login():
    probe = AuditSearchProbe(receipt_provider=lambda *_: receipt(),
                             search=search(Cluster([login_line(T0 + timedelta(seconds=1), user="chat_owner")]),
                                           now=T0 + timedelta(minutes=4)),
                             source_binding=BINDING, streams=("target-dev/target-db-1/postgres",),
                             evidence_sink=lambda _: None, clock=lambda: T0 + timedelta(minutes=4))
    assert probe(CONTEXT, SPEC).observed == "absent"


@pytest.mark.parametrize("bad", [{"namespace": "Target"}, {"pod": "a/b"}, {"container": ""}, {"source_binding": ""}])
def test_the_source_is_registered(bad):
    args = {"namespace": "target-dev", "pod": "target-db-1", "container": "postgres", "source_binding": BINDING}
    with pytest.raises(ValueError, match="registered pod log source"):
        PodLogAuditSearch(Cluster().read, **(args | bad))
