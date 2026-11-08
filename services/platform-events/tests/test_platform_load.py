"""Platform load (T49 demo): one Job per plan in SHIFT..WATCH under a delegated SHIFT, its log windows received."""

import json

from test_approval_delegation import SPEC, delegate, world  # noqa: F401  (fixture)

from bravo_platform_events.platform_load import PlatformLoad, job_name, windows_from_log
from bravo_platform_events.window_store import _binding


class Kube:
    def __init__(self):
        self.jobs, self.created, self.log = {}, [], b""

    def __call__(self, args, payload=None):
        if args[:2] == ["get", "job"]:
            job = self.jobs.get(args[2])
            return (0, json.dumps(job).encode(), b"") if job else (1, b"", b"NotFound")
        if args[:3] == ["create", "-f", "-"]:
            job = json.loads(payload)
            self.jobs[job["metadata"]["name"]] = job | {"status": {"active": 1}}
            self.created.append(job)
            return 0, b"job.batch/" + job["metadata"]["name"].encode(), b""
        if args[0] == "logs":
            return 0, self.log, b""
        raise AssertionError(args)


def _state(store, state):
    with store._transaction(write=True, plan_id=SPEC.plan_id) as c:
        c.execute("UPDATE plans SET state=?, version=version+1 WHERE plan_id=?", (state, SPEC.plan_id))


def test_windows_are_only_the_marked_log_lines():
    assert windows_from_log('noise\nBRAVO_WINDOW {"a": 1}\nBRAVO_WINDOW not-json\nBRAVO_WINDOW {"b": 2}\n') == [
        {"a": 1}, {"b": 2}]


def test_a_delegated_shift_gets_one_load_job_and_its_windows_under_the_current_binding(world):  # noqa: F811
    store, kube = world["store"], Kube()
    load = PlatformLoad(store, kube, world["delegations"], image="ghcr.io/softbank-hackathon-finals/platform-product-reader@sha256:" + "a" * 64,
                        base_url="https://edge.example.invalid", source_env="aws", target_env="onprem",
                        clock=world["clock"])
    received = []
    load.intake.receive = lambda producer, window: received.append((producer.credential_id, window))
    _state(store, "SHIFT")
    assert load.sweep() == {}  # nothing delegated: a person runs the load
    delegate(world)  # shift + cutover
    kube.log = b'BRAVO_WINDOW {"window_start": "w1"}\nother\n'
    [outcome] = load.sweep().values()
    assert outcome["job"] == job_name(SPEC.plan_id, "load") and outcome["received"] == 1
    [job] = kube.created
    command = job["spec"]["template"]["spec"]["containers"][0]["command"]
    assert command[:4] == ["python", "-m", "bravo_loadgen.cli", "run"]
    assert "--window-stdout" in command and command[command.index("--generator") + 1] == "platform-job"
    pod = job["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "initial-proposal" and "volumeMounts" not in pod["containers"][0]
    with store._transaction() as c:
        bound = _binding(c, SPEC.plan_id, "platform-load")
    assert (bound["plan_state"], bound["enabled"]) == ("SHIFT", True)
    load.sweep()
    assert len(kube.created) == 1  # still running: never a second Job
    _state(store, "WATCH")  # a new state and version: bound again, same Job keeps the load
    load.sweep()
    with store._transaction() as c:
        rebound = _binding(c, SPEC.plan_id, "platform-load")
    assert (rebound["plan_state"], rebound["revision"]) == ("WATCH", bound["revision"] + 1)
    assert len(kube.created) == 1 and received[-1][0].startswith("platform-load-")
    kube.jobs[job_name(SPEC.plan_id, "load")]["status"] = {"succeeded": 1}
    load.sweep()
    assert len(kube.created) == 2  # ended while WATCH still needs load: replaced
