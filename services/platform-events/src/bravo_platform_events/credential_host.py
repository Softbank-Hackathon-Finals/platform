"""Installed T10/T15 composition; all execution inputs are protected deployment mounts.

The stored PROPOSE result is the only proposal source. Namespace management and
verification Jobs use different renewable kubeconfigs. No HTTP input selects
credentials, modules, images, paths, cluster identity or a success verdict.
"""

import importlib.util
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from bravo_agent.verification import Kube
from rehearsal_runner.credential_job import CredentialFixture
from rehearsal_runner.credential_planner import CredentialPlanner
from rehearsal_runner.isolation.kube import Kubectl
from rehearsal_runner.isolation.profile import read_env, targets
from rehearsal_runner.stored_proposals import StoredProposalSource
from rehearsal_runner.t9_delivery import load_t9_delivery

from .initial_proposal import InitialProposalExecution
from .product_read_server import configured_initial_reader
from .rehearsal_server import RehearsalPorts, _json

RUNNER = "system:serviceaccount:bravo-rehearsal-control:t15-runner"
_FIELDS = {"manager_subject", "reader_config", "runtime_source_sha", "target_environment_id", "cluster_uid",
           "manager_kubeconfig", "runner_kubeconfig", "profile_file", "isolation_image",
           "t9_renderer", "scoped_lifecycle_module", "fixture", "candidate_repository", "staging"}


class _StoredOnly:
    def recipient(self, *args):
        raise RuntimeError("stored_proposal_host_cannot_dispatch")

    submit = read = validate = recipient


class ConfiguredKube(Kube):
    def __init__(self, path):
        super().__init__()
        self.path = str(path)

    def call(self, *args, data=None):
        return super().call("--kubeconfig", self.path, "--request-timeout=30s", *args, data=data)


@dataclass(frozen=True)
class ConfiguredKubectl(Kubectl):
    path: str = ""

    def run(self, *args, stdin=None, timeout=60):
        return super().run("--kubeconfig", self.path, "--request-timeout=30s", *args,
                           stdin=stdin, timeout=timeout)


def _connection(kube):
    try:
        config = json.loads(kube.call("config", "view", "--minify", "-o", "json"))
        clusters = config["clusters"]
        if len(clusters) != 1:
            raise ValueError()
        connection = clusters[0]["cluster"]
        url = urlsplit(connection["server"])
        if (url.scheme != "https" or not url.hostname or url.username or url.password
                or url.query or url.fragment or connection.get("insecure-skip-tls-verify", False) is not False
                or connection.get("proxy-url")):
            raise ValueError()
        return connection
    except Exception:
        raise ValueError("rehearsal_cluster_connection_unconfirmed") from None


def _clients(path, identity):
    kube, kubectl = ConfiguredKube(path), ConfiguredKubectl(path=str(path))
    try:
        who = json.loads(kube.call("auth", "whoami", "-o", "json"))
        if who["status"]["userInfo"]["username"] != identity:
            raise ValueError()
    except Exception:
        raise ValueError("rehearsal_client_identity_unconfirmed") from None
    return kube, kubectl


@dataclass(frozen=True)
class StoredCredentialPlanner:
    execution: InitialProposalExecution
    target_environment_id: str
    candidate_repository: str
    scoped_type: object
    provisioner: object
    cleaner: object
    settings: dict

    def binding(self, admission):
        value = self.execution.rehearsal_binding(admission)
        if value.target_environment_id != self.target_environment_id:
            raise ValueError("rehearsal_target_mismatch")
        return value

    def factory(self, admission, task):
        self.binding(admission)
        # A per-admission closure prevents an unrelated plan's publication from
        # supplying a candidate to this Job, even when their diff bytes match.
        def image(diff_hash):
            self.binding(admission)
            return self.candidate_repository + "@" + self.execution.rehearsal_image(admission, diff_hash)

        planner = CredentialPlanner(**self.settings, bindings=self.binding, candidate_image=image)
        return self.scoped_type(planner, provisioner=self.provisioner,
                                cleaner=self.cleaner).factory(admission, task)


def create_ports(store, config_file="/rehearsal/ports.json"):
    config = _json(config_file)
    if (type(config) is not dict or set(config) != _FIELDS
            or re.fullmatch(r"[a-f0-9]{40}", config["runtime_source_sha"]) is None
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", config["target_environment_id"]) is None
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9./:_-]*", config["candidate_repository"]) is None
            or re.fullmatch(r"system:serviceaccount:bravo-rehearsal-control:[a-z0-9-]+", config["manager_subject"]) is None
            or config["manager_subject"] == RUNNER
            or not isinstance(config["cluster_uid"], str) or not config["cluster_uid"]):
        raise ValueError("invalid_credential_host_configuration")
    names = ("reader_config", "manager_kubeconfig", "runner_kubeconfig", "profile_file",
             "t9_renderer", "scoped_lifecycle_module", "staging")
    paths = {name: Path(config[name]) for name in names}
    if (any(not path.is_absolute() for path in paths.values())
            or any(not path.is_file() for name, path in paths.items() if name != "staging")
            or not paths["staging"].is_dir()
            or paths["manager_kubeconfig"].resolve() == paths["runner_kubeconfig"].resolve()):
        raise ValueError("credential_host_mounts_required")
    fixture_config = config["fixture"]
    if type(fixture_config) is not dict or set(fixture_config) != {
            "bao_image", "postgres_image", "probe_image", "openbao_ca_file"}:
        raise ValueError("credential_host_fixture_required")
    ca = Path(fixture_config["openbao_ca_file"])
    if not ca.is_absolute() or not ca.is_file():
        raise ValueError("credential_host_ca_required")
    fixture = CredentialFixture(**{k: v for k, v in fixture_config.items() if k != "openbao_ca_file"},
                                openbao_ca_pem=ca.read_text())
    profile = read_env(paths["profile_file"])
    targets(profile)
    delivery = load_t9_delivery(paths["t9_renderer"])
    spec = importlib.util.spec_from_file_location("t7_scoped_lifecycle", paths["scoped_lifecycle_module"])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reader = configured_initial_reader(store, paths["reader_config"])
    execution = InitialProposalExecution(reader.records, _StoredOnly(), principal=reader.caller.principal,
                                         runtime_source_sha=config["runtime_source_sha"])
    # Only authenticated management can read the fixed cluster UID; the runner
    # deliberately lacks global namespace permission. Neither client writes here.
    manager, provisioner = _clients(paths["manager_kubeconfig"], config["manager_subject"])
    job_kube, job_kubectl = _clients(paths["runner_kubeconfig"], RUNNER)
    if _connection(manager) != _connection(job_kube):
        raise ValueError("rehearsal_clients_target_mismatch")
    if manager.get("namespace", "kube-system")["metadata"]["uid"] != config["cluster_uid"]:
        raise ValueError("rehearsal_cluster_mismatch")
    proposals = StoredProposalSource(execution.rehearsal_input, execution.rehearsal_result, paths["staging"])
    planner = StoredCredentialPlanner(execution, config["target_environment_id"],
        config["candidate_repository"], module.ScopedPlanner, provisioner, manager,
        dict(profile=profile, proposals=proposals, kubectl=job_kubectl, kube=job_kube,
             isolation_image=config["isolation_image"], fixture=fixture, delivery=delivery, cleaner=manager))
    return RehearsalPorts(reader.records.catalog, planner, first_run=execution.rehearsal_run_id)
