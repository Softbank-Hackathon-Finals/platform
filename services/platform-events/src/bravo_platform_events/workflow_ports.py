"""Deployment ports for the workflow host (`workflow_host_server --ports ...workflow_ports:create_ports`).

One protected JSON (`/workflow/ports.json`, a ConfigMap) names mounted files only; no secret values.

    {"version": 1,
     "executor": {"url": "https://...", "ca_file": "/executor/ca.crt", "token_file": "/executor/read-token",
                  "registered_pair": {"source": {...}, "target": {...}}},
     "propose": {"reader_config": "/product-read/reader.json", "principal": "<T10 initial grant principal>",
                 "runtime_source_sha": "<40 hex: the runtime the AI execution was claimed with>",
                 "selections_dir": "/workflow/selections", "demo_chat_clone": "/workflow-state/demo-chat",
                 "github_app": {"app_id": 1, "key_file": "/github-app/private-key.pem",
                                "workflow_sha": "<demo-chat main after #25>", "workflow_id": 408240730,
                                "state_file": "/workflow-state/dispatches.json"},
                 "auto": {"state_dir": "/workflow-state/initial-operator", "relay_config": "/workflow/relay.json",
                          "catalog_root": "/catalog",
                          "gitops_image": "/workflow/product-reader-image.json"}},
     "watch": {"profile_file": "/workflow/sli-profile.json", "retest": "module:factory"},
     "application": "module:factory"}

`executor` and `propose` are required for the happy path (PROPOSE, AWAIT_APPROVAL, CUTOVER).
`github_app`, `auto` (T49: admit the one registered task and run the AI Job without an operator),
`watch` and `application` are optional; an absent section leaves its states holding
as workflow_reader_unwired. The Executor client is read-only (no command token): the host only
reconciles; SHIFT approval and cutover submission stay with the operator API.
"""

import importlib
import json
import re
import ssl
import subprocess
from pathlib import Path

HEX40 = re.compile(r"[a-f0-9]{40}")
FACTORY = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")
FIELDS = {"version", "executor", "propose", "watch", "application"}
REQUIRED = {"version", "executor", "propose"}


def _path(value, *, directory=False):
    path = Path(value)
    if not path.is_absolute() or not (path.is_dir() if directory else path.is_file()):
        raise ValueError(f"workflow_mount_required:{value}")
    return path


def _factory(spec):
    if not isinstance(spec, str) or FACTORY.fullmatch(spec) is None:
        raise ValueError("workflow_factory_invalid")
    module, name = spec.split(":")
    return getattr(importlib.import_module(module), name)


def _executor(config):
    from .executor import ExecutorClient

    if set(config) != {"url", "ca_file", "token_file", "registered_pair"} or not config["url"].startswith("https://"):
        raise ValueError("workflow_executor_configuration_invalid")
    context = ssl.create_default_context(cafile=str(_path(config["ca_file"])))  # CERT_REQUIRED + hostname
    token = _path(config["token_file"]).read_text().strip()
    client = ExecutorClient(config["url"], token=token, tls_context=context)
    return client, client.verified_bindings(config["registered_pair"])


def _clone(path):
    """An empty local repository the writer fetches into (public demo-chat, no credential)."""
    path = Path(path)
    if not path.is_absolute() or not path.parent.is_dir():
        raise ValueError("workflow_clone_mount_required")
    if not (path / ".git").exists() and not (path / "HEAD").exists():
        subprocess.run(["git", "init", "--quiet", "--bare", str(path)], check=True, timeout=30)
    return path


def _propose(store, config):
    from .credential_host import _StoredOnly
    from .github_app import GitHubApp
    from .initial_proposal import InitialProposalExecution
    from .product_read_server import configured_initial_reader
    from .workflow_propose import pinned_selections, propose_preparer

    allowed = {"reader_config", "principal", "runtime_source_sha", "selections_dir", "demo_chat_clone", "github_app", "auto"}
    optional = {"github_app", "auto"}
    if set(config) - allowed or allowed - optional - set(config) or not HEX40.fullmatch(config["runtime_source_sha"]):
        raise ValueError("workflow_propose_configuration_invalid")
    reader = configured_initial_reader(store, str(_path(config["reader_config"])))
    # The host only reads stored results and publications: the stored-only client never submits.
    execution = InitialProposalExecution(reader.records, _StoredOnly(), principal=config["principal"],
                                         runtime_source_sha=config["runtime_source_sha"])
    clone = _clone(config["demo_chat_clone"])
    github, writer = None, None
    app = config.get("github_app")
    if app is not None:
        from .candidate_publication import CandidateGitHub
        from .candidate_writer import WRITER_PERMISSIONS, CandidateWriter

        if (set(app) != {"app_id", "key_file", "workflow_sha", "workflow_id", "state_file"}
                or not HEX40.fullmatch(app["workflow_sha"]) or type(app["workflow_id"]) is not int):
            raise ValueError("workflow_github_app_configuration_invalid")
        key = _path(app["key_file"]).read_bytes()
        repository = "Softbank-Hackathon-Finals/demo-chat"
        github = CandidateGitHub(app=GitHubApp(app["app_id"], key, repository, {"actions": "read"}))
        writer = CandidateWriter(GitHubApp(app["app_id"], key, repository, WRITER_PERMISSIONS), clone,
                                 workflow_sha=app["workflow_sha"], workflow_id=app["workflow_id"],
                                 state_file=app["state_file"])
    auto = None
    if "auto" in config:
        # T49: the host admits the plan's one registered task and runs the AI Job itself (no operator relay).
        from . import initial_proposal_job as job
        from .initial_proposal_auto import AutoInitialProposal

        spec = config["auto"]
        if set(spec) != {"state_dir", "relay_config", "catalog_root", "gitops_image"}:
            raise ValueError("workflow_propose_auto_configuration_invalid")
        image, revision = job.gitops_image(_path(spec["gitops_image"]))
        if revision != config["runtime_source_sha"]:
            raise ValueError("workflow_propose_auto_runtime_mismatch")  # the Job must run the claimed runtime
        auto = AutoInitialProposal(reader.records, relay_config=json.loads(_path(spec["relay_config"]).read_text()),
                                   state_dir=_path(spec["state_dir"], directory=True), image=image,
                                   source_revision=revision, principal=config["principal"],
                                   catalog_root=_path(spec["catalog_root"], directory=True))
    return propose_preparer(execution, select=pinned_selections(config["selections_dir"]),
                            repository=clone, github=github, writer=writer, auto=auto)


def create_ports(store, config_file="/workflow/ports.json"):
    from .cutover import CutoverService
    from .workflow_stages import integrity_reader, shift_approval_reader, sli_observer, watch_reader

    config = json.loads(_path(config_file).read_text())
    if type(config) is not dict or set(config) - FIELDS or REQUIRED - set(config) or config["version"] != 1:
        raise ValueError("workflow_ports_configuration_invalid")
    client, bindings = _executor(config["executor"])
    cutover = CutoverService(store, client, bindings=bindings)
    readers = {"AWAIT_APPROVAL": shift_approval_reader, "CUTOVER": integrity_reader}
    preparers = {"PROPOSE": _propose(store, config["propose"])}
    if "watch" in config:
        watch = config["watch"]
        if set(watch) != {"profile_file", "retest"}:
            raise ValueError("workflow_watch_configuration_invalid")
        profile = json.loads(_path(watch["profile_file"]).read_text())
        readers["WATCH"] = watch_reader(observe=sli_observer(profile), retest=_factory(watch["retest"])(store))
    if "application" in config:
        # Owner-provided (T31/T19): returns {"REHEARSE": preparer, "GATE": preparer} built with
        # workflow_stages.application_stages(..., host_subject="workflow-host") over this process's
        # single ApplicationHost; that subject needs its own apply_change grant.
        stages = _factory(config["application"])(store)
        if not isinstance(stages, dict) or set(stages) - {"REHEARSE", "GATE"}:
            raise ValueError("workflow_application_ports_invalid")
        preparers |= stages
    return {"cutover": cutover, "readers": readers, "preparers": preparers}
