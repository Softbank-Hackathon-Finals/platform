"""Inputs and adapters for the existing T3 and T15 operator fixtures."""

import hashlib
import importlib.util
import json
import re
from pathlib import Path

from .journal import Failure, kubectl, locked, save

REPO = Path(__file__).resolve().parents[2]
IMAGES = (
    "candidate_image",
    "bao_image",
    "postgres_image",
    "probe_image",
    "isolation_image",
)
SOURCE_PATHS = ("app/db_secret.py", "app/config.py", "app/main.py")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def profile(document):
    required = {
        "schema",
        "cluster_uid",
        "architecture",
        "images",
        "source_sha",
        "sources",
        "proposal",
        "diff_hash",
        "isolation_profile",
    }
    if set(document) != required or document["schema"] != "bravo.team-runtime/v1":
        raise Failure("environment_schema_invalid")
    from uuid import UUID

    UUID(document["cluster_uid"])
    if document["architecture"] not in ("arm64", "amd64"):
        raise Failure("architecture_invalid")
    if set(document["images"]) != set(IMAGES) or any(
        not re.fullmatch(
            r"[A-Za-z0-9./:_-]+@sha256:[a-f0-9]{64}", document["images"][key]
        )
        for key in IMAGES
    ):
        raise Failure("immutable_images_required")
    if (
        set(document["sources"]) != set(SOURCE_PATHS)
        or not re.fullmatch(r"[a-f0-9]{40}", document["source_sha"])
        or not re.fullmatch(r"sha256:[a-f0-9]{64}", document["diff_hash"])
    ):
        raise Failure("source_binding_invalid")
    edits = document["proposal"].get("edits", [])
    if (
        not edits
        or len({e["path"] for e in edits}) != len(edits)
        or any(
            set(e) != {"path", "replacement"} or e["path"] not in SOURCE_PATHS
            for e in edits
        )
    ):
        raise Failure("proposal_invalid")
    return document


def read_environment(namespace, name):
    response = kubectl(["get", "configmap", name, "-n", namespace, "-o", "json"])
    if response.returncode:
        raise Failure("environment_unavailable")
    obj = json.loads(response.stdout)
    config = profile(json.loads(obj["data"]["environment.json"]))
    return config, {
        "namespace": namespace,
        "name": name,
        "uid": obj["metadata"]["uid"],
        "resource_version": obj["metadata"]["resourceVersion"],
        "sha256": hashlib.sha256(obj["data"]["environment.json"].encode()).hexdigest(),
    }


def preflight(config, credentials):
    def get(args):
        result = kubectl(args)
        if result.returncode:
            raise Failure("preflight_read_unavailable")
        return json.loads(result.stdout)

    system = get(["get", "namespace", "kube-system", "-o", "json"])
    if system["metadata"]["uid"] != config["cluster_uid"]:
        raise Failure("cluster_identity_mismatch")
    nodes = get(["get", "nodes", "-o", "json"])["items"]
    eligible = [n for n in nodes if not n.get("spec", {}).get("unschedulable")]
    if not eligible or any(
        n["status"]["nodeInfo"]["architecture"] != config["architecture"]
        for n in eligible
    ):
        raise Failure("environment_architecture_mismatch")
    keys = IMAGES if credentials else ("isolation_image",)
    # Local image imports are installation state. Check all eligible nodes before a trial.
    # Remote pinned images are checked by an actual Pod pull and execution, not cached metadata.
    for key in keys:
        image = config["images"][key]
        if image.startswith("localhost/") and any(
            not any(
                image in row.get("names", []) for row in n["status"].get("images", [])
            )
            for n in eligible
        ):
            raise Failure("environment_local_image_missing:" + key)
    return {
        "cluster_matches": True,
        "architecture": config["architecture"],
        "eligible_nodes": len(eligible),
    }


def write_delivery(root, config):
    # The T9 functions keep their original module globals; align its image with the writer.
    original = root / "target-render.py"
    with locked(root) as record:
        expected = record.get("renderer_sha256")
        if expected is None:
            original.write_bytes(
                (REPO / "infra/envs/target-secrets/render.py").read_bytes()
            )
            record["renderer_sha256"] = hashlib.sha256(
                original.read_bytes()
            ).hexdigest()
        elif (
            not original.is_file()
            or hashlib.sha256(original.read_bytes()).hexdigest() != expected
        ):
            raise Failure("frozen_renderer_missing_or_changed")
    (root / "delivery.py").write_text(
        "import importlib.util\n"
        + f"s = importlib.util.spec_from_file_location('target_render', {str(original)!r})\n"
        + "m = importlib.util.module_from_spec(s)\ns.loader.exec_module(m)\n"
        + f"m.BAO_IMAGE = {config['images']['bao_image']!r}\n"
        + "for name in ('rehearsal_remote_path', 'rehearsal_role_name', 'rehearsal_auth_role', 'rehearsal_fetch_init_container'):\n"
        + "    globals()[name] = getattr(m, name)\n"
    )


def prepare(root, config):
    source = root / "source"
    for path, text in config["sources"].items():
        output = source / path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text)
    save(root / "proposal.json", config["proposal"])
    (root / "isolation.env").write_text(config["isolation_profile"])
    ca = kubectl(
        [
            "get",
            "configmap",
            "target-openbao-ca",
            "-n",
            "bravo-rehearsal-control",
            "-o",
            "json",
        ]
    )
    if ca.returncode:
        raise Failure("openbao_ca_unavailable")
    pem = json.loads(ca.stdout)["data"]["ca.crt"]
    if "BEGIN CERTIFICATE" not in pem:
        raise Failure("openbao_ca_invalid")
    (root / "openbao-ca.pem").write_text(pem)
    write_delivery(root, config)
    cfg = {
        "state": str(root),
        "cluster_uid": config["cluster_uid"],
        "source_dir": str(source),
        "source_sha": config["source_sha"],
        "proposal": str(root / "proposal.json"),
        "openbao_ca_file": str(root / "openbao-ca.pem"),
        "t9_render": str(root / "delivery.py"),
        "profile": str(root / "isolation.env"),
        "probe_from_source": True,
        **config["images"],
    }
    harness = load_module(
        "credential_harness",
        REPO / "services/rehearsal-runner/testing/rke2_credential_flow.py",
    )
    diff, _ = harness.proposal_diff(cfg)
    if "sha256:" + hashlib.sha256(diff).hexdigest() != config["diff_hash"]:
        raise Failure("candidate_diff_binding_mismatch")
    save(root / "config.json", cfg)
    return cfg


def isolation(root, config):
    import secrets

    from bravo_agent.verification import Kube
    from rehearsal_runner.isolation.__main__ import run
    from rehearsal_runner.isolation.fixtures import Fixtures
    from rehearsal_runner.isolation.kube import Kubectl
    from rehearsal_runner.isolation.model import exit_code
    from rehearsal_runner.isolation.profile import read_env, targets
    from rehearsal_runner.namespace_lifecycle import NamespaceLifecycle

    p = root / "isolation.env"
    p.write_text(config["isolation_profile"])
    values = read_env(p)
    life = NamespaceLifecycle(Kubectl(), Kube(), values)
    ns = life.create(root.name)
    fx = Fixtures(ns.name, secrets.token_hex(6), config["images"]["isolation_image"])
    try:
        results = run(Kubectl(), fx, targets(values))
        code = exit_code(results)
        save(
            root / "isolation.json",
            {"exit_code": code, "results": [r.to_dict() for r in results]},
        )
        return "PASS" if code == 0 else "FAIL" if code == 1 else "UNCONFIRMED"
    finally:
        fx.cleanup(Kubectl())
        life.cleanup(ns)


def credentials(root, config):
    cfg = prepare(root, config)
    harness = load_module(
        "credential_harness",
        REPO / "services/rehearsal-runner/testing/rke2_credential_flow.py",
    )
    code = harness.run(cfg)
    reports = list(root.glob("t15-credential-flow-*.json"))
    if len(reports) != 1:
        raise Failure("credential_report_missing")
    report = json.loads(reports[0].read_text())
    # Preserve the harness's global-leftovers exit and report. Our separate cleanup
    # verdict is limited to objects created by this run.
    return report["t10_status"], {"harness_exit": code, "report": reports[0].name}
