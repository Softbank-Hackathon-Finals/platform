"""Apply an approved plan (ADR-0033 3.4). No LLM import; every decision here is code.

Order of a run:
1. Read the target identity and the configuration repository head, re-render the plan.
2. Refuse unless the T10 `approval_kind=infra` binding has exactly this `plan_digest`; then claim
   the approval once for this run (T10 checks the digest, expiry, revocation and grants again).
3. Run the step graph. Steps whose needs are done start in parallel; a failure, hold or
   operator wait stops only the steps that depend on it.

A run is bound to what was approved, not to the files of the moment:
- the stack it executes is the copy stored at start (its digest is inside `plan_digest`);
- every step first re-reads the target identity, so a switched kubeconfig or context holds;
- every resume asks T10 again (revocation, the caller's own `apply_infra` grant) before moving on;
- a publish holds when anyone else moved the configuration repository.

There is no delete: no prune, uninstall or Secret overwrite. An unobservable result is HOLD,
never retried silently. Secret values exist only in memory on their way to the cluster.
"""

import json
import secrets
import string
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime

from .applications import application, application_name, project
from .plan import Planner
from .stack import StackError, canonical, environment_of, sha256_text, substitute
from .terraform import Scratch, TerraformCli, inputs_digest, outputs_reader, plan_step

PENDING, RUNNING, SKIPPED, APPLIED = "PENDING", "RUNNING", "SKIPPED", "APPLIED"
WAITING, FAILED, HOLD = "WAITING_OPERATOR", "FAILED", "HOLD"
DONE = frozenset({SKIPPED, APPLIED})


class StepOutcome(Exception):
    """A step ended without converging. `status` is FAILED, HOLD or WAITING_OPERATOR."""

    def __init__(self, status, reason):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _now():
    return datetime.now(UTC).isoformat()


def _random(length):
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _steps(run):
    return {step["id"]: step for step in run["stack"]["steps"]}


class Executor:
    def __init__(self, planner: Planner, *, stack_revision, cluster, repo, approvals, checks, store,
                 repo_url, ledger=None, clock=_now, sleep=time.sleep, monotonic=time.monotonic,
                 poll_seconds=5.0, workers=4, identity=None, terraform=None):
        self.planner, self.stack = planner, planner.stack
        self.stack_revision, self.repo_url = stack_revision, repo_url
        self.cluster, self.repo, self.approvals, self.checks, self.store = cluster, repo, approvals, checks, store
        self.ledger = ledger or (lambda event: None)
        self.clock, self.sleep, self.monotonic = clock, sleep, monotonic
        self.poll, self.workers = poll_seconds, workers
        # What a run is bound to: the cluster and node by default, an AWS account for terraform stacks.
        self.identity = identity or (lambda params: cluster.identity(params["node_name"]))
        self.terraform = terraform or TerraformCli()
        # One lock for the run record and the repository: workers mutate `run` and push commits
        # while the scheduler saves it, so every mutation and every save happens under it.
        self._lock = threading.RLock()

    # --- entry points -------------------------------------------------------------------------

    def start(self, *, request, migration_plan_id, approval_id, run_id, started_by):
        if self.store.exists(run_id):
            return self.store.load(run_id)
        target = self.identity(request["params"])
        head = self.repo.head() if self.repo is not None else "0" * 40  # a stack without publish steps
        plan = self.planner.plan(request, stack_revision=self.stack_revision, target=target, base_commit=head)
        run = dict(run_id=run_id, migration_plan_id=migration_plan_id, approval_id=approval_id,
                   plan_digest=plan.plan_digest, plan=plan.document, stack=self.stack.document,
                   files=plan.files, head=head, commits={}, confirmations={}, started_by=started_by,
                   created_at=self.clock(), status=PENDING,
                   steps={s["id"]: {"status": PENDING} for s in plan.document["steps"]})
        approval = self.approvals.read(migration_plan_id, approval_id)
        if approval.get("binding", {}).get("plan_digest") != plan.plan_digest:
            return self._refuse(run, "plan_digest_mismatch")
        if approval.get("revoked") or approval.get("claimed_run_id") not in (None, run_id):
            return self._refuse(run, "approval_unusable")
        claim = self.approvals.claim(migration_plan_id, approval_id, run_id=run_id, plan_digest=plan.plan_digest)
        run["claim"] = {k: claim[k] for k in ("approved_by", "claimed_by", "claimed_at") if k in claim}
        self.store.save(run)
        self.ledger({"event": "infra.run.started", "run_id": run_id, "plan_digest": plan.plan_digest,
                     "approval_id": approval_id, "at": run["created_at"]})
        return self._execute(run)

    def confirm(self, run_id, step_id, *, confirmed_by):
        """A human says an operator_action is done. The confirm check still runs before moving on."""
        run = self.store.load(run_id)
        step = _steps(run).get(step_id) if "stack" in run else None
        if step is None or step["kind"] != "operator_action":
            raise StackError("not_an_operator_step")
        if run["status"] in ("REFUSED", "SUCCEEDED"):
            return run
        run["confirmations"][step_id] = {"by": confirmed_by, "at": self.clock()}
        if run["steps"][step_id]["status"] == WAITING:
            run["steps"][step_id] = {"status": PENDING}
        self.store.save(run)
        return self.resume(run_id)

    def resume(self, run_id):
        run = self.store.load(run_id)
        if run["status"] in ("REFUSED", "SUCCEEDED"):
            return run
        if sha256_text(canonical(run.get("stack"))) != run["plan"]["stack_digest"]:
            return self._hold(run, "stored_stack_mismatch")
        # Ask T10 again: a revoked approval stops the run, and the caller (whose token this
        # client carries) must hold `apply_infra` now. Replaying the same run's claim is idempotent.
        approval = self.approvals.read(run["migration_plan_id"], run["approval_id"])
        if approval.get("revoked") or approval.get("claimed_run_id") != run["run_id"]:
            return self._hold(run, "approval_revoked")
        self.approvals.claim(run["migration_plan_id"], run["approval_id"], run_id=run["run_id"],
                             plan_digest=run["plan_digest"])
        if self.repo is not None and self.repo.head() != run["head"]:
            return self._hold(run, "config_repo_moved")
        for state in run["steps"].values():
            if state["status"] in (RUNNING, WAITING, HOLD):
                state["status"] = PENDING  # re-observe; DONE steps are never re-applied
        return self._execute(run)

    # --- scheduling ---------------------------------------------------------------------------

    def _refuse(self, run, reason):
        run["status"], run["reason"] = "REFUSED", reason
        self.store.save(run)
        self.ledger({"event": "infra.run.refused", "run_id": run["run_id"], "reason": reason, "at": self.clock()})
        return run

    def _hold(self, run, reason):
        run["status"], run["reason"] = HOLD, reason
        self.store.save(run)
        self.ledger({"event": "infra.run.status", "run_id": run["run_id"], "status": HOLD, "reason": reason,
                     "at": self.clock()})
        return run

    def _execute(self, run):
        steps, states = _steps(run), run["steps"]
        run.pop("reason", None)

        def runnable(step_id):
            return states[step_id]["status"] == PENDING and all(
                states[n]["status"] in DONE for n in steps[step_id].get("needs", []))

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {}
            while True:
                with self._lock:
                    for step_id in steps:
                        if step_id not in futures.values() and runnable(step_id):
                            states[step_id] = {"status": RUNNING, "started_at": self.clock()}
                            futures[pool.submit(self._run_step, run, steps[step_id])] = step_id
                    self.store.save(run)
                if not futures:
                    break
                finished, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in finished:
                    step_id = futures.pop(future)
                    try:
                        status, reason = future.result(), None
                    except StepOutcome as outcome:
                        status, reason = outcome.status, outcome.reason
                    except StackError as error:
                        status, reason = FAILED, str(error)
                    except Exception:  # an adapter error is not a verdict: hold for a human
                        status, reason = HOLD, "adapter_error"
                    with self._lock:
                        states[step_id].update(status=status, ended_at=self.clock())
                        if reason:
                            states[step_id]["reason"] = reason
                        self.ledger({"event": "infra.step", "run_id": run["run_id"], "step": step_id,
                                     "status": status, "reason": reason, "at": states[step_id]["ended_at"]})
        with self._lock:
            statuses = {s["status"] for s in states.values()}
            for status in (FAILED, HOLD, WAITING):
                if status in statuses:
                    run["status"] = status
                    break
            else:
                run["status"] = "SUCCEEDED" if statuses <= DONE else PENDING
            run["updated_at"] = self.clock()
            self.store.save(run)
        self.ledger({"event": "infra.run.status", "run_id": run["run_id"], "status": run["status"],
                     "at": run["updated_at"]})
        return run

    def _run_step(self, run, step):
        # Every step starts on the approved cluster, whatever the kubeconfig says now.
        if self.identity(run["plan"]["request"]["params"]) != run["plan"]["target"]:
            raise StepOutcome(HOLD, "target_identity_changed")
        # A cloud stack builds its cluster in this run, so the run is bound to the account or project;
        # before any cluster step the kubeconfig's API server must be the one terraform reported.
        if (step["kind"] != "terraform" and "cluster_endpoint" in environment_of(run["stack"])
                and step.get("confirm") != "cluster-access" and self.checks.check("cluster-access", run) is not True):
            raise StepOutcome(HOLD, "target_cluster_unbound")
        return getattr(self, "_" + step["kind"])(run, step)

    # --- step kinds ---------------------------------------------------------------------------

    def _probe(self, run, step):
        if self.checks.check(step["check"], run) is True:
            return APPLIED
        raise StepOutcome(FAILED, "check_failed")

    def _operator_action(self, run, step):
        observed = self.checks.check(step["confirm"], run)
        confirmed = step["id"] in run["confirmations"]
        if observed is True:
            return APPLIED if confirmed else SKIPPED
        if observed is None and confirmed:  # not observable by code; the human confirmation stands
            return APPLIED
        raise StepOutcome(WAITING, "confirm_check_failed" if confirmed else "operator_action_required")

    def _gitops_publish(self, run, step):
        files = run["files"][step["id"]]
        stack = run["stack"]["stack"]
        name = application_name(stack, step["path"])
        with self._lock:
            current = (self.cluster.application_status(name) or {}).get("revision")
            if current and self.repo.read_tree(step["path"], current) == files:
                commit, status = current, SKIPPED  # Argo CD already follows these exact files
            else:
                if self.repo.head() != run["head"]:
                    raise StepOutcome(HOLD, "config_repo_moved")  # someone else published meanwhile
                if self.repo.read_tree(step["path"]) == files:
                    commit, status = run["head"], SKIPPED
                else:
                    commit = self.repo.replace_tree(step["path"], files, expected_head=run["head"],
                                                    message=f"[T45] {run['run_id']} {step['id']}")
                    run["head"], status = commit, APPLIED
            run["commits"][step["path"]] = commit
            self.store.save(run)  # the new head is durable before anything else can fail
        published = {k: v for k, v in run["files"].items() if _steps(run)[k]["kind"] == "gitops_publish"}
        manifests = [project(stack, repo_url=self.repo_url, files=published),
                     application(stack, step["path"], repo_url=self.repo_url, revision=commit)]
        if self.cluster.diff(manifests):
            self.cluster.apply(manifests)
            status = APPLIED
        return status

    def _gitops_sync(self, run, step):
        path = _steps(run)[step["publish"]]["path"]
        with self._lock:
            expected = run["commits"].get(path)
        name = application_name(run["stack"]["stack"], path)
        if expected is None:
            raise StepOutcome(HOLD, "publish_commit_unknown")
        deadline = self.monotonic() + step["timeout_seconds"]
        while True:
            status = self.cluster.application_status(name)
            if status and status.get("revision") == expected and status.get("sync") == "Synced" \
                    and status.get("health") == "Healthy":
                return APPLIED
            if self.monotonic() >= deadline:
                raise StepOutcome(HOLD, "sync_timeout")
            self.sleep(self.poll)

    def _terraform(self, run, step):
        """Re-plan; apply only the change set a person approved, never a delete, replace or forget."""
        params = run["plan"]["request"]["params"]
        root = self.planner.repo_root
        approved = _planned(run, step)
        if inputs_digest(root, step, params) != approved["inputs"]:
            raise StepOutcome(HOLD, "terraform_inputs_changed")  # code or a private input moved since approval
        steps = {s["id"]: s for s in run["stack"]["steps"] if s["kind"] == "terraform"}
        read = outputs_reader(self.terraform, root, steps, params)
        with Scratch() as scratch:
            planned = plan_step(self.terraform, root, step, params, read, scratch, lock=True)
            if not planned["changed"]:
                return SKIPPED
            with self._lock:
                run.setdefault("terraform", {})[step["id"]] = {"changes": planned["changes"],
                                                               "destructive": len(planned["destructive"])}
            if approved["change_set"] in ("UNPLANNED", "PENDING_UPSTREAM"):
                raise StepOutcome(HOLD, "terraform_change_set_unapproved")  # plan again now that upstream exists
            if planned["change_set"] != approved["change_set"]:
                raise StepOutcome(HOLD, "terraform_change_set_changed")
            if planned["destructive"]:
                raise StepOutcome(HOLD, "terraform_plan_destroys")  # a person decides deletes and replaces
            self.terraform.apply(planned["directory"], scratch / "data", scratch / "plan.bin")
            return APPLIED

    def _render_apply(self, run, step):
        items = [item for text in run["files"][step["id"]].values() for item in _items(text)]
        if not self.cluster.diff(items):
            return SKIPPED
        self.cluster.apply(items)
        return APPLIED

    def _secret_generate(self, run, step):
        created = False
        for secret in substitute(step["secrets"], _params(run)):
            if self.cluster.secret_exists(secret["namespace"], secret["name"]):
                continue  # never overwrite: the consumer may already use the value
            values = {}
            for key, spec in secret["keys"].items():
                if "literal" in spec:
                    values[key] = spec["literal"]
                elif "random" in spec:
                    values[key] = _random(spec["random"])
                else:
                    raise StackError("unknown_generator")
            self.cluster.create_secret(secret["namespace"], secret["name"], secret["type"], values)
            created = True
        return APPLIED if created else SKIPPED

    def _wait(self, run, step):
        """The condition must hold continuously for `stable_seconds`: a Ready flag can flip before the
        Service in front of it routes (kind measurement: CNPG Ready, then the migration's connect refused)."""
        target, stable = substitute(step["target"], _params(run)), step.get("stable_seconds", 0)
        deadline = self.monotonic() + step["timeout_seconds"]
        since = None
        while True:
            now = self.monotonic()
            if self.cluster.condition(target["kind"], target["namespace"], target["name"], step["condition"]):
                since = now if since is None else since
                if now - since >= stable:
                    return APPLIED
            else:
                since = None
            if now >= deadline:
                raise StepOutcome(HOLD, "wait_timeout")
            self.sleep(self.poll)


def _params(run):
    return run.get("plan", {}).get("request", {}).get("params", {})


def _planned(run, step):
    return next(s for s in run["plan"]["steps"] if s["id"] == step["id"])


def _items(text):
    return json.loads(text)["items"]
