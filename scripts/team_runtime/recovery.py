"""Read back the same run; recover scoped OpenBao cleanup before deleting inputs."""

import json
import time

from .journal import LABEL, Failure, deletion, kubectl, locked, match, read

DELETED = {"login": "allow", "role": "deleted", "value": "deleted"}


def inspect(root):
    with locked(root) as record:
        observed = []
        for ref in record["resources"]:
            obj = read(ref)
            if obj is None:
                observed.append({**ref, "state": "ABSENT"})
            else:
                ref["uid"] = match(obj, ref, record["run_id"])
                observed.append(
                    {
                        **ref,
                        "state": "DELETING"
                        if obj["metadata"].get("deletionTimestamp")
                        else "PRESENT",
                    }
                )
        record["observed"] = observed
    return observed


def delete_one(ref, run_id):
    obj = read(ref)
    if obj is None:
        return
    uid = match(obj, ref, run_id)
    result = kubectl(
        ["delete", "--raw", deletion(ref), "-f", "-"],
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": uid},
                "propagationPolicy": "Foreground",
            }
        ),
    )
    if result.returncode:
        raise Failure("uid_delete_unconfirmed")
    for _ in range(45):
        obj = read(ref)
        if obj is None:
            return
        match(obj, ref, run_id)
        time.sleep(1)
    raise Failure("deletion_pending_requery_same_run")


def external_cleanup(root, config):
    """Only this run's namespace-derived paths. Existing cleanup jobs are observed, not replayed."""
    from bravo_agent.verification import Kube
    from rehearsal_runner.credential_job import (
        CLEANUP_SCRIPT,
        CONTROL_NAMESPACE,
        WRITER_ACCOUNT,
        WRITER_LABEL,
        CredentialFixture,
        CredentialJob,
    )
    from rehearsal_runner.isolation.kube import Kubectl
    from rehearsal_runner.t9_delivery import load_t9_delivery

    record = json.loads((root / "journal.json").read_text())
    resources = record["resources"]
    writers = [
        x for x in resources if x["kind"] == "Job" and x["name"].endswith("-write")
    ]
    if not writers or record.get("external_cleanup") == DELETED:
        return
    reports = list(root.glob("t15-credential-flow-*.json"))
    if len(reports) == 1:
        body = json.loads(reports[0].read_text())
        cleanup = (
            (body.get("record", {}).get("evidence") or {})
            .get("observations", {})
            .get("cleanup")
        )
        if cleanup == DELETED:
            with locked(root) as value:
                value["external_cleanup"] = cleanup
            return
    # No writer may still be producing values after the recovery cleanup. Foreground
    # deletion waits for its dependent Pod, then UID-bound cleanup may run.
    for ref in writers:
        delete_one(ref, record["run_id"])
    namespaces = [
        x
        for x in resources
        if x["kind"] == "Namespace" and x["name"].startswith("rehearsal-")
    ]
    if len(namespaces) != 1:
        raise Failure("credential_namespace_ambiguous")
    ns = namespaces[0]["name"]
    from .recipes import write_delivery

    write_delivery(root, config)
    delivery = load_t9_delivery(root / "delivery.py")
    fixture = CredentialFixture(
        **{
            k: config["images"][k]
            for k in ("bao_image", "postgres_image", "probe_image")
        },
        openbao_ca_pem=(root / "openbao-ca.pem").read_text(),
    )
    job = CredentialJob(Kube(), Kubectl(), None, None, delivery, fixture, lambda _: "")
    work = "t15-cred-" + ns + "-recover"
    meta = {"namespace": CONTROL_NAMESPACE, "labels": {LABEL: record["run_id"]}}

    def ensure(kind, name, fields):
        current = json.loads((root / "journal.json").read_text())
        refs = [
            r for r in current["resources"] if r["kind"] == kind and r["name"] == name
        ]
        if refs:
            obj = read(refs[0])
            if obj is None:
                raise Failure("recovery_input_missing_no_blind_recreate")
            match(obj, refs[0], record["run_id"])
        else:
            Kube().create(
                {
                    "apiVersion": "v1",
                    "kind": kind,
                    "metadata": {**meta, "name": name},
                    **fields,
                }
            )

    ensure(
        "Secret",
        work,
        {
            "stringData": {
                "remote": delivery.remote_path(ns),
                "role-name": delivery.role_name(ns),
            }
        },
    )
    ensure(
        "ConfigMap",
        work + "-ca",
        {"immutable": True, "data": {"ca.crt": fixture.openbao_ca_pem}},
    )
    current = json.loads((root / "journal.json").read_text())
    refs = [r for r in current["resources"] if r["kind"] == "Job" and r["name"] == work]
    if refs:
        obj = read(refs[0])
        if obj is None:
            raise Failure("recovery_job_missing_no_blind_replay")
        match(obj, refs[0], record["run_id"])
        if not any(
            c["type"] == "Complete" and c["status"] == "True"
            for c in obj.get("status", {}).get("conditions", [])
        ):
            raise Failure("recovery_job_pending_inspect_same_run")
        # Observe the exact completed job's Pod/image before accepting its output.
        from bravo_agent.verification import observe_job_pod

        execution = observe_job_pod(Kube(), obj, fixture.bao_image, complete=True)
        result = json.loads(
            Kube()
            .call("logs", "pod/" + execution["pod_name"], "-n", CONTROL_NAMESPACE)
            .strip()
        )
    else:
        result, _ = job._job(
            CONTROL_NAMESPACE,
            work,
            fixture.bao_image,
            CLEANUP_SCRIPT,
            WRITER_ACCOUNT,
            meta,
            {"login", "role", "value"},
            work=work,
            role=WRITER_ACCOUNT,
            labels=WRITER_LABEL,
        )
    if result != DELETED:
        raise Failure("external_cleanup_unconfirmed")
    with locked(root) as value:
        value["external_cleanup"] = result


def cleanup(root, config):
    inspect(
        root
    )  # Resolve lost create responses by exact name + run marker, never list-prefix adoption.
    record = json.loads((root / "journal.json").read_text())
    if record["suite"] == "credentials":
        external_cleanup(root, config)
    record = json.loads((root / "journal.json").read_text())
    # Jobs first; Namespace last. Control secrets remain until Bao cleanup is proven.
    order = {"Job": 0, "Secret": 1, "ConfigMap": 2, "Namespace": 3}
    for ref in sorted(record["resources"], key=lambda r: order[r["kind"]]):
        delete_one(ref, record["run_id"])
    observed = inspect(root)
    state = "CLEAN" if all(r["state"] == "ABSENT" for r in observed) else "PENDING"
    with locked(root) as value:
        value["cleanup"] = state
    return state
