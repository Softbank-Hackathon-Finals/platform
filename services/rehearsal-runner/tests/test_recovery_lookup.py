import copy

import pytest

from rehearsal_runner.recovery_lookup import (
    ATTEMPT_LABEL,
    RUN_ANNOTATION,
    RUN_LABEL,
    LiveNamespaceCandidate,
    RecoveryUnavailable,
    find_live_namespace,
    recovery_identity,
)


class FakeKube:
    def __init__(self, items):
        self.items, self.calls = items, []

    def get_json(self, *args):
        self.calls.append(args)
        return {"items": self.items}


def item(run="run-1", attempt=2):
    labels, annotations = recovery_identity(run, attempt)
    labels["app.kubernetes.io/part-of"] = "bravo-rehearsal"
    return {"metadata": {"name": "rehearsal-" + "a" * 24, "uid": "uid-1", "labels": labels,
                         "annotations": annotations}}


def test_live_candidate_is_found_by_run_hash_and_attempt():
    kube = FakeKube([item()])
    result = find_live_namespace(kube, "run-1", 2)
    assert result == LiveNamespaceCandidate("rehearsal-" + "a" * 24, "uid-1")
    assert kube.calls == [("namespaces", "-l", f"{RUN_LABEL}={recovery_identity('run-1', 2)[0][RUN_LABEL]},{ATTEMPT_LABEL}=2")]


def test_empty_lookup_does_not_claim_cleanup():
    assert find_live_namespace(FakeKube([]), "run-1", 2) is None


@pytest.mark.parametrize("fault", ["multiple", "wrong-run", "wrong-attempt", "missing-uid", "deleting", "wrong-owner"])
def test_ambiguous_or_changed_candidate_is_unavailable(fault):
    value = copy.deepcopy(item())
    meta = value["metadata"]
    if fault == "wrong-run":
        meta["annotations"][RUN_ANNOTATION] = "other-run"
    elif fault == "wrong-attempt":
        meta["labels"][ATTEMPT_LABEL] = "1"
    elif fault == "missing-uid":
        meta["uid"] = ""
    elif fault == "deleting":
        meta["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    elif fault == "wrong-owner":
        meta["labels"]["app.kubernetes.io/part-of"] = "other"
    values = [value, copy.deepcopy(value)] if fault == "multiple" else [value]
    with pytest.raises(RecoveryUnavailable):
        find_live_namespace(FakeKube(values), "run-1", 2)


@pytest.mark.parametrize("run_id,attempt", [("../bad", 1), ("run-1", True), ("run-1", 4)])
def test_invalid_rehearsal_identity_rejected_before_cluster_read(run_id, attempt):
    kube = FakeKube([])
    with pytest.raises(ValueError):
        find_live_namespace(kube, run_id, attempt)
    assert kube.calls == []


def test_job_lookup_rechecks_namespace_uid_and_does_not_interpret_result():
    from rehearsal_runner.recovery_lookup import find_live_jobs

    namespace = item()["metadata"]
    labels, annotations = recovery_identity("run-1", 2)
    job = {"metadata": {"name": "verifier", "uid": "job-uid-1", "namespace": namespace["name"],
                        "labels": labels, "annotations": annotations}}
    class Kube:
        def __init__(self):
            self.calls = []
        def get_json(self, *args):
            self.calls.append(args)
            return {"items": [job]} if args[0] == "jobs" else {"metadata": namespace}
    kube = Kube()
    result = find_live_jobs(kube, LiveNamespaceCandidate(namespace["name"], namespace["uid"]), "run-1", 2)
    assert [(job.name, job.uid) for job in result] == [("verifier", "job-uid-1")]
    assert [call[0] for call in kube.calls] == ["namespace", "jobs", "namespace"]
    assert not hasattr(result[0], "verdict")


def test_namespace_uid_change_during_job_lookup_rejects_candidates():
    from rehearsal_runner.recovery_lookup import find_live_jobs

    metadata = item()["metadata"]
    class Kube:
        def __init__(self):
            self.reads = 0
        def get_json(self, *args):
            if args[0] == "jobs":
                return {"items": []}
            self.reads += 1
            return {"metadata": metadata | {"uid": "uid-other" if self.reads == 2 else "uid-1"}}
    with pytest.raises(RecoveryUnavailable):
        find_live_jobs(Kube(), LiveNamespaceCandidate(metadata["name"], "uid-1"), "run-1", 2)


@pytest.mark.parametrize("fault", ["wrong-run", "wrong-attempt", "wrong-namespace", "missing-uid", "duplicate"])
def test_untrusted_or_ambiguous_job_candidates_are_rejected(fault):
    from rehearsal_runner.recovery_lookup import find_live_jobs

    namespace = item()["metadata"]
    labels, annotations = recovery_identity("run-1", 2)
    meta = {"name": "verifier", "uid": "job-uid-1", "namespace": namespace["name"],
            "labels": labels, "annotations": annotations}
    if fault == "wrong-run":
        meta["annotations"][RUN_ANNOTATION] = "other-run"
    elif fault == "wrong-attempt":
        meta["labels"][ATTEMPT_LABEL] = "1"
    elif fault == "wrong-namespace":
        meta["namespace"] = "other"
    elif fault == "missing-uid":
        meta["uid"] = ""
    jobs = [{"metadata": meta}]
    if fault == "duplicate":
        jobs.append(copy.deepcopy(jobs[0]))
    class Kube:
        def get_json(self, *args):
            return {"items": jobs} if args[0] == "jobs" else {"metadata": namespace}
    with pytest.raises(RecoveryUnavailable):
        find_live_jobs(Kube(), LiveNamespaceCandidate(namespace["name"], namespace["uid"]), "run-1", 2)
