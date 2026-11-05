"""Resource sizing rejects missing/foreign samples and counter resets."""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("resource_stats", Path(__file__).parents[1] / "testing/stats.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
TARGET = module.Target("bravo-observability", "bravo-pgbouncer-exporter", "exporter")
CID = "a" * 64


def inventory():
    return [
        {"kind": "Deployment", "metadata": {"name": TARGET.deployment, "uid": "dep"}, "spec": {"replicas": 1}},
        {"kind": "ReplicaSet", "metadata": {"name": "rs", "uid": "rs", "ownerReferences": [
            {"kind": "Deployment", "uid": "dep", "controller": True}]}},
        {"kind": "Pod", "metadata": {"name": "pod", "uid": "pod-uid", "ownerReferences": [
            {"kind": "ReplicaSet", "uid": "rs", "controller": True}]}, "status": {
                "phase": "Running", "containerStatuses": [{"name": "exporter", "ready": True,
                    "state": {"running": {}}, "containerID": f"containerd://{CID}"}]}},
    ]


def cri():
    return {"attributes": {"id": CID, "labels": {
        "io.kubernetes.container.name": TARGET.container,
        "io.kubernetes.pod.namespace": TARGET.namespace,
        "io.kubernetes.pod.name": "pod", "io.kubernetes.pod.uid": "pod-uid",
    }}, "cpu": {"usageCoreNanoSeconds": {"value": "100"}, "timestamp": "1000"},
        "memory": {"workingSetBytes": {"value": "300"}}}


def install(monkeypatch, items=None, rows=None):
    items = inventory() if items is None else items
    rows = [cri()] if rows is None else rows
    def read(_node, *args):
        if args[0] == "kubectl":
            assert args == ("kubectl", "get", "deployments,replicasets,pods", "-n", TARGET.namespace, "-o", "json")
            return json.dumps({"items": items})
        if args[:2] == ("crictl", "stats"):
            return json.dumps({"stats": rows})
        if args[:2] == ("crictl", "inspect"):
            assert args[-1] == CID
            return "123"
        if args[-1] == "/proc/123/cgroup":
            return "0::/kubepods/test.scope"
        if args[-1] == "/sys/fs/cgroup/kubepods/test.scope/memory.stat":
            return "anon 200\nfile 100\n"
        raise AssertionError(args)
    monkeypatch.setattr(module, "node_exec", read)


def test_namespace_owner_chain_and_cri_identity_match(monkeypatch):
    foreign = cri()
    foreign["attributes"]["labels"]["io.kubernetes.pod.namespace"] = "other"
    install(monkeypatch, rows=[foreign, cri()])
    assert module.sample("node", [TARGET])[TARGET] == (("dep", "pod-uid", "pod", CID), 300, 100, 1000, 200)


@pytest.mark.parametrize("label", ["io.kubernetes.pod.namespace", "io.kubernetes.pod.name",
                                  "io.kubernetes.pod.uid", "io.kubernetes.container.name"])
def test_foreign_sample_cannot_fill_a_missing_target(monkeypatch, label):
    row = cri()
    row["attributes"]["labels"][label] = "foreign"
    install(monkeypatch, rows=[row])
    with pytest.raises(module.Unobservable, match="CRI_SAMPLE_MISSING"):
        module.sample("node", [TARGET])


@pytest.mark.parametrize("case", ["no_pod", "wrong_owner", "two_pods", "not_ready", "two_replicas"])
def test_absent_ambiguous_or_unready_workload_rejected(monkeypatch, case):
    items = inventory()
    if case == "no_pod":
        items.pop()
    elif case == "wrong_owner":
        items[-1]["metadata"]["ownerReferences"][0]["uid"] = "foreign-rs"
    elif case == "two_pods":
        items.append(copy.deepcopy(items[-1]))
    elif case == "not_ready":
        items[-1]["status"]["containerStatuses"][0]["ready"] = False
    else:
        items[0]["spec"]["replicas"] = 2
    install(monkeypatch, items=items)
    with pytest.raises(module.Unobservable):
        module.sample("node", [TARGET])


def test_duplicate_cri_rows_rejected(monkeypatch):
    install(monkeypatch, rows=[cri(), cri()])
    with pytest.raises(module.Unobservable, match="AMBIGUOUS"):
        module.sample("node", [TARGET])


def samples():
    identity = ("dep", "pod", "pod-name", CID)
    return [{TARGET: (identity, 10, 1_000_000, 1_000_000_000, 5)},
            {TARGET: (identity, 30, 3_000_000, 2_000_000_000, 20)}]


def test_valid_cpu_uses_cri_time_and_includes_initial_memory_peak():
    rows = samples()
    rows[0][TARGET] = (rows[0][TARGET][0], 2**20, 1_000_000, 1_000_000_000, 2**19)
    result = module.summarize(rows, [TARGET])[0]
    assert "VALID" in result and "1.0MiB" in result and "2.0m" in result


@pytest.mark.parametrize("case", ["replace", "reset", "stale", "backwards", "one"])
def test_replacement_reset_or_stale_interval_never_reports_cpu(case):
    rows = samples()
    identity, mem, cpu, timestamp, anon = rows[1][TARGET]
    if case == "replace":
        identity = ("dep", "new-pod", "new-name", "b" * 64)
    elif case == "reset":
        cpu = 0
    elif case == "stale":
        timestamp = 1_000_000_000
    elif case == "backwards":
        timestamp = 0
    else:
        rows.pop()
    if len(rows) > 1:
        rows[1][TARGET] = (identity, mem, cpu, timestamp, anon)
    with pytest.raises(module.Unobservable):
        module.summarize(rows, [TARGET])


def test_pod_replacement_during_read_is_unknown(monkeypatch):
    install(monkeypatch)
    inv = inventory()
    replacement = copy.deepcopy(inv)
    replacement[-1]["metadata"]["uid"] = "replacement"
    calls = iter([{TARGET.namespace: inv}, {TARGET.namespace: replacement}])
    monkeypatch.setattr(module, "inventory", lambda *_: next(calls))
    with pytest.raises(module.Unobservable, match="IDENTITY_CHANGED_DURING_SAMPLE"):
        module.sample("node", [TARGET])


def test_cli_missing_sample_is_nonzero_and_never_success(monkeypatch, capsys):
    install(monkeypatch, items=[], rows=[])
    assert module.main(["node", "1", "1", str(TARGET)]) == 2
    output = capsys.readouterr().out
    assert "UNKNOWN" in output and "VALID" not in output and "평균 CPU" not in output


def test_cli_opaque_subprocess_errors_do_not_disclose_raw_details(monkeypatch, capsys):
    def fail(*_):
        raise module.Unobservable("NODE_READ_FAILED")
    monkeypatch.setattr(module, "node_exec", fail)
    assert module.main(["node", "1", "1", str(TARGET)]) == 2
    assert capsys.readouterr().out.strip() == "UNKNOWN resource interval: NODE_READ_FAILED"


def test_both_capacity_phases_use_qualified_targets():
    shell = (Path(__file__).parents[1] / "testing/k8s.sh").read_text()
    assert 'stats.py "$NAME" 60 5 "${CAPACITY_TARGETS[@]}"' in shell
    assert 'stats.py "$NAME" 180 5 "${CAPACITY_TARGETS[@]}"' in shell
    assert '"$NS/bravo-pgbouncer-exporter/exporter"' in shell


@pytest.mark.parametrize("case", ["replace", "reset", "stale", "missing"])
def test_cli_interval_failure_cannot_print_partial_success(monkeypatch, capsys, case):
    rows = samples()
    identity, memory, cpu, timestamp, anon = rows[1][TARGET]
    if case == "replace":
        identity = ("dep", "replacement", "replacement", "b" * 64)
    elif case == "reset":
        cpu = 0
    elif case == "stale":
        timestamp = rows[0][TARGET][3]
    rows[1][TARGET] = (identity, memory, cpu, timestamp, anon)
    calls = iter(rows)
    def read(*_):
        row = next(calls)
        if case == "missing" and row is rows[1]:
            raise module.Unobservable("CRI_SAMPLE_MISSING_OR_AMBIGUOUS")
        return row
    monkeypatch.setattr(module, "sample", read)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    assert module.main(["node", "1", "1", str(TARGET)]) == 2
    output = capsys.readouterr().out
    assert "UNKNOWN" in output and "VALID" not in output and "평균 CPU" not in output
