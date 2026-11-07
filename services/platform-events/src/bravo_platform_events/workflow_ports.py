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
                          "gitops_image": "/workflow/product-reader-image.json", "plans": ["<plan id>"]}},
     "watch": {"profile_file": "/workflow/sli-profile.json", "retest": "module:factory",
               "target_controls": "/workflow/target-controls.json"},
     "application": "module:factory"}

`executor` and `propose` are required for the happy path (PROPOSE, AWAIT_APPROVAL, CUTOVER).
`github_app`, `auto` (T49: admit the one registered task and run the AI Job without an operator),
`watch`, `application` and `delegation` (true: record pre-delegated approvals, T49) are optional;
an absent section leaves its states holding
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
FIELDS = {"version", "executor", "propose", "watch", "application", "delegation", "deferred", "load"}
REQUIRED = {"version", "executor", "propose"}


def _path(value, *, directory=False):
    path = Path(value)
    if not path.is_absolute() or not (path.is_dir() if directory else path.is_file()):
        raise ValueError(f"workflow_mount_required:{value}")
    return path


def _state_dir(value):
    """An absolute directory on the host's state volume; created on first start (it did not exist yet on the
    first live rollout, 11/8, and the host would not start)."""
    path = Path(value)
    if not path.is_absolute() or not path.parent.is_dir():
        raise ValueError(f"workflow_mount_required:{value}")
    if path.is_dir() and not path.is_symlink():
        # The kubelet's fsGroup pass adds group bits to the volume on every mount, so the 0700 directory the
        # ledger requires is 0770 again after a restart (live 11/8: the host stopped at the second start).
        path.chmod(0o700)
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
        required = {"state_dir", "relay_config", "catalog_root", "gitops_image"}
        if (not required <= set(spec) <= required | {"plans"}
                or ("plans" in spec and (type(spec["plans"]) is not list or not spec["plans"]))):
            raise ValueError("workflow_propose_auto_configuration_invalid")
        image, revision = job.gitops_image(_path(spec["gitops_image"]))
        if revision != config["runtime_source_sha"]:
            raise ValueError("workflow_propose_auto_runtime_mismatch")  # the Job must run the claimed runtime
        auto = AutoInitialProposal(reader.records, relay_config=json.loads(_path(spec["relay_config"]).read_text()),
                                   # The Job ledger makes its own private (0700) directory on first use.
                                   state_dir=_state_dir(spec["state_dir"]), image=image,
                                   source_revision=revision, principal=config["principal"],
                                   catalog_root=_path(spec["catalog_root"], directory=True),
                                   plans=spec.get("plans"))  # absent: every plan on this host
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
    from .plan_run_state import analysis_reader

    # ANALYZE reads the S1 records (T16 collection, first T19 report); nothing to configure.
    readers = {"ANALYZE": analysis_reader, "AWAIT_APPROVAL": shift_approval_reader, "CUTOVER": integrity_reader}
    preparers = {"PROPOSE": _propose(store, config["propose"])}
    delegations = None
    if config.get("delegation") is True:
        # T49: approvals the approver delegated in advance are recorded by this host at the gate.
        from .approval_delegation import ApprovalDelegations
        from .operator_access import OperatorAccess
        from .workflow_stages import ShiftApprovalService, delegated_shift_preparer

        access = OperatorAccess(store)
        delegations = ApprovalDelegations(store, access)
        shift = ShiftApprovalService(store, check_approve=access.requires("approve_shift"))
        readers.pop("AWAIT_APPROVAL")
        preparers["AWAIT_APPROVAL"] = delegated_shift_preparer(shift, delegations)
    if "watch" in config:
        watch = config["watch"]
        if set(watch) - {"target_controls"} != {"profile_file", "retest"}:
            raise ValueError("workflow_watch_configuration_invalid")
        profile = json.loads(_path(watch["profile_file"]).read_text())
        observe = sli_observer(profile)
        readers["WATCH"] = watch_reader(observe=observe, retest=_factory(watch["retest"])(store))
        if delegations is not None:
            from .workflow_stages import watch_fail_preparer

            preparers["WATCH"] = watch_fail_preparer(store, observe, readers.pop("WATCH"))
        if "target_controls" in watch:
            # ADR-0041: the platform records the post-cutover target T14 run the retest compares.
            from .target_controls import create as target_controls
            from .target_controls import watch_preparer

            reader = readers.pop("WATCH", None)
            inner = preparers.pop("WATCH", None) or (lambda *_: reader)
            preparers["WATCH"] = watch_preparer(target_controls(store, _path(watch["target_controls"])), inner)
    if "application" in config:
        # Owner-provided (T31/T19): returns {"REHEARSE": preparer, "GATE": preparer} built with
        # workflow_stages.application_stages(..., host_subject="workflow-host") over this process's
        # single ApplicationHost; that subject needs its own apply_change grant.
        factory = _factory(config["application"])
        stages = factory(store, delegations=delegations) if delegations else factory(store)
        if not isinstance(stages, dict) or set(stages) - {"REHEARSE", "GATE"}:
            raise ValueError("workflow_application_ports_invalid")
        preparers |= stages
    deferred = config.get("deferred", [])  # host states this deployment declares it does not drive yet
    if type(deferred) is not list:
        raise ValueError("workflow_deferred_invalid")
    background = []
    if "load" in config:
        # T49: platform load for SHIFT..WATCH (platform_load), only where approvals are delegated.
        spec = config["load"]
        keys = {"image_file", "base_url", "source_env", "target_env"}
        if delegations is None or not keys <= set(spec) <= keys | {"write_rps", "read_rps", "duration"}:
            raise ValueError("workflow_load_configuration_invalid")
        from . import initial_proposal_job as job
        from .initial_proposal_auto import InClusterKubectl
        from .platform_load import PlatformLoad

        image, _ = job.gitops_image(_path(spec["image_file"]))
        background.append(PlatformLoad(store, InClusterKubectl(), delegations, image=image, base_url=spec["base_url"],
                                       source_env=spec["source_env"], target_env=spec["target_env"],
                                       **{k: spec[k] for k in ("write_rps", "read_rps", "duration") if k in spec}).sweep)
    return {"cutover": cutover, "readers": readers, "preparers": preparers, "deferred": deferred,
            "background": background}
