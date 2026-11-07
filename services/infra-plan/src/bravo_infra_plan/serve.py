"""Host entrypoint for the T45 API with T10's operator identity.

People are verified exactly as the T10 approval host verifies them (`operator_verifier`: the team's
IAM Identity Center proof for the operator audience, or pinned OIDC access tokens). The same bearer
token is then forwarded to T10, so T10 checks that person's own `apply_infra` grant and the
approval's revocation on every start and resume. Nothing here mints, stores or logs a token.

No agent identity is accepted yet: an agent credential needs a workload identity (T38) that this
host can tell apart from a person. Until then agents cannot reach the API at all, which is stricter
than ADR-0033 (agents may create and read plans).
"""

import argparse
import json
import subprocess
from pathlib import Path

from .adapters import (
    AuditLedger,
    ClusterChecks,
    FilePlanStore,
    FileRunStore,
    GitConfigRepo,
    KubectlCluster,
    T10Approvals,
)
from .api import Principal, create_app
from .executor import Executor
from .plan import HelmChartRenderer, Planner
from .stack import load_stack

FIELDS = frozenset({"version", "operator_verifier", "t10_url", "stack", "repo_root", "charts_dir", "kubeconfig",
                    "config_repo", "config_branch", "config_workdir", "argo_repo_url", "runs_dir", "plans_dir",
                    "audit_sink"})


def operator_verifier(operator):
    """Build the same verifier the T10 approval host builds from the same configuration shape."""
    from bravo_platform_events.operator_identity import (
        OPERATOR_SSO_AUDIENCE,
        OperatorSsoVerifier,
        OperatorTokenVerifier,
    )

    people = operator if isinstance(operator, list) else [operator]
    if people and all(isinstance(o, dict) and "expected_sso_role_arn" in o for o in people):
        from bravo_platform_events.registration_sso import SsoRegistrationVerifier

        return OperatorSsoVerifier([SsoRegistrationVerifier(**(o | {"audience": OPERATOR_SSO_AUDIENCE}))
                                    for o in people])
    return OperatorTokenVerifier(**operator)


def operator_resolver(verifier):
    """A verified person becomes a human principal that carries its own token on to T10."""
    def resolve(request):
        person = verifier(request)
        if person is None:
            return None
        token = request.headers["authorization"].partition(" ")[2]
        return Principal(person.subject_id, "human", token)
    return resolve


def build(config):
    if type(config) is not dict or set(config) != FIELDS or config["version"] != 1:
        raise ValueError("invalid_infra_plan_host_configuration")
    stack = load_stack(config["stack"])
    repo_root = Path(config["repo_root"]).resolve()
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo_root, check=True, capture_output=True,
                              text=True).stdout.strip()
    planner = Planner(stack, repo_root, chart_renderer=HelmChartRenderer(config["charts_dir"]))
    cluster = KubectlCluster(config["kubeconfig"])
    repo = GitConfigRepo(config["config_repo"], config["config_branch"], config["config_workdir"])
    runs = FileRunStore(config["runs_dir"])
    if config["audit_sink"]:  # T24 hash chain, the production record
        append = AuditLedger(config["audit_sink"], runs)
    else:
        ledger = Path(config["runs_dir"]) / "ledger.jsonl"

        def append(event):
            with open(ledger, "a") as handle:
                handle.write(json.dumps(event, sort_keys=True) + "\n")

    def executor_for(token):
        return Executor(planner, stack_revision=revision, cluster=cluster, repo=repo,
                        approvals=T10Approvals(config["t10_url"], token), checks=ClusterChecks(cluster, repo_root),
                        store=runs, repo_url=config["argo_repo_url"], ledger=append)

    return create_app(stack=stack, plans=FilePlanStore(config["plans_dir"]), executor_for=executor_for,
                      resolve_principal=operator_resolver(operator_verifier(config["operator_verifier"])))


def main(argv=None):
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--tls-cert", required=True)
    parser.add_argument("--tls-key", required=True)
    args = parser.parse_args(argv)
    app = build(json.loads(Path(args.config).read_text()))
    uvicorn.run(app, host="0.0.0.0", port=8443, workers=1, ssl_certfile=args.tls_cert, ssl_keyfile=args.tls_key,
                access_log=False, proxy_headers=False, server_header=False, limit_concurrency=16)


if __name__ == "__main__":
    main()
