"""Approved Edge drill: measure weight reflection, real distribution and automatic 0% outside a SHIFT.

A migration plan reaches SHIFT only after GATE. T17's own acceptance (reflection time, distribution,
SLI abort) needs the real LB and real T18 load before that. The drill keeps every T17 safeguard and
changes only where the human decision comes from:

- A human approves the exact drill spec (`spec_digest`: plan, run, T36 profile, LB) once, as a T10
  `approval_kind=infra` approval on a plan that has not reached SHIFT (requester != approver, <= 24 h).
  The requester may be the agent that prepared the spec; the approving human may then run it (ADR-0039,
  2026-11-08 amendment: a demo drill needs one present human, not two).
- `register` claims that approval once for this run (`apply_infra`; the approver must still hold
  `approve_infra`). Every later step re-reads approval, claim, revocation, expiry, plan version and
  both grants under the plan lock, as ShiftAuthority does for a SHIFT approval.
- The same ShiftHost path runs it: the one LB journal, Controller PATCH/GET, the plan's stored T18
  windows, T36 `judge` with SHIFT rules and automatic 0%. A plan in SHIFT or later is refused, so a
  drill never runs beside a migration's own traffic shift. Nothing here changes a plan state or a DB.
"""

import argparse
import hashlib
import json
import signal
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from threading import Event

from bravo_platform_events.infra_approval import (
    INFRA_APPROVAL_STATES,
    InfraApprovalService,
    infra_key,
)
from bravo_platform_events.operator_access import OperatorAccess

from .control import _now
from .host import CurrentShift, ShiftHost, _digest, _get, _key, _time
from .provider import Binding, Cloudflare, EdgeError
from .runtime import ShiftRuntime

KIND = "edge-drill"


def drill_spec(*, plan_id, run_id, profile, lb_binding):
    """What the human approves. Paths, tokens and the approval id are not part of the decision."""
    return dict(kind=KIND, version=1, plan_id=plan_id, run_id=run_id, profile=profile, lb_binding=lb_binding)


def spec_digest(spec):
    return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _profile_valid(profile, plan_id, clock):
    from bravo_observability.sli import judge

    judge(profile, None, plan_id=plan_id, state="SHIFT", weight_target=0,
          observe_from=clock(), decided_at=clock(), prior_unknown=0)


class DrillAuthority:
    def __init__(self, store, *, spec, approval_id, clock):
        if type(spec) is not dict or spec.get("kind") != KIND or not callable(clock):
            raise ValueError("invalid_drill_configuration")
        if not isinstance(approval_id, str) or not 1 <= len(approval_id) <= 128:
            raise ValueError("invalid_drill_configuration")
        Binding(**spec["lb_binding"])
        self.store, self.spec, self.approval_id, self.clock = store, spec, approval_id, clock
        self.run_id, self.digest = spec["run_id"], spec_digest(spec)
        self.access = OperatorAccess(store, clock=clock)
        self.approvals = InfraApprovalService(
            store, check_approve=self.access.requires("approve_infra"),
            check_apply=self.access.requires("apply_infra"), check_access=self.access.check_read,
            check_approver=self.access.requires_subject("approve_infra"), clock=clock)

    def register(self, principal, plan_id, *, run_id, profile, binding):
        """Claim the human's approval for this run once. A replay returns the same claim."""
        if (plan_id != self.spec["plan_id"] or run_id != self.run_id or profile != self.spec["profile"]
                or not isinstance(binding, Binding) or asdict(binding) != self.spec["lb_binding"]):
            raise EdgeError("drill_spec_mismatch")
        _profile_valid(profile, plan_id, self.clock)
        # T10 compares the whole binding. The drill fixes the plan id (its run) and the spec digest;
        # the spec names no requester, so the requester is the one T10 recorded (approver != it).
        requested_by = self.approvals.read(principal, plan_id, self.approval_id)["binding"]["requested_by"]
        binding = dict(infra_plan_id=run_id, plan_digest=self.digest, requested_by=requested_by)
        return self.approvals.claim(principal, plan_id, self.approval_id, run_id=run_id, binding=binding)

    def proof(self, c, plan):
        """The approval and its claim for this drill, valid now; otherwise refuse."""
        plan_id = plan["plan_id"]
        approval = _get(c, plan_id, infra_key("approval", self.approval_id))
        claim = _get(c, plan_id, infra_key("consumed", self.approval_id))
        if approval is None or claim is None:
            raise EdgeError("drill_approval_unavailable")
        receipt, claimed = approval[1], claim[1]
        if (receipt["approval_kind"] != "infra" or receipt["binding"]["plan_digest"] != self.digest
                or claimed["run_id"] != self.run_id or claimed["binding"]["plan_digest"] != self.digest
                or receipt["plan_version"] != plan["version"] or plan["state"] not in INFRA_APPROVAL_STATES
                or _get(c, plan_id, infra_key("operator-revoked-approval", self.approval_id))
                or _time(receipt["expires_at"]) <= _time(self.clock())):
            raise EdgeError("drill_approval_unavailable")
        return dict(approval=receipt, claim=claimed)

    def __call__(self, c, principal, plan):
        executor = self.access.require(c, principal, plan, "apply_infra")
        proof = self.proof(c, plan)
        approver = self.access._require_subject(c, plan, proof["approval"]["actor_id"], "approve_infra",
                                                _time(self.clock()).timestamp())
        revision = _digest(dict(proof=proof, executor=executor, approver=approver))
        return CurrentShift(self.approval_id, self.run_id, revision, self.spec["profile"], self.spec["lb_binding"])


class DrillHost(ShiftHost):
    """ShiftHost whose current authority is a claimed drill approval instead of a SHIFT checkpoint."""

    def __init__(self, store, provider, journal, *, authority, clock):
        if not isinstance(authority, DrillAuthority):
            raise ValueError("drill_authority_required")
        super().__init__(store, provider, journal, authority=authority, clock=clock)

    def _current(self, c, principal, plan_id):
        row = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None or plan_id != self.authority.spec["plan_id"]:
            raise EdgeError("drill_plan_required")
        plan = dict(row)
        current = self.authority(c, principal, plan)
        if not isinstance(current, CurrentShift):
            raise EdgeError("current_shift_authority_required")
        if current.lb_binding != asdict(self.provider.binding):
            raise EdgeError("registered_lb_binding_mismatch")
        # Read again here, independent of the authority object, like the SHIFT checkpoint.
        proof = self.authority.proof(c, plan)
        _profile_valid(current.profile, plan_id, self.clock)
        return plan, current, proof


class ProofStream:
    """Operator principal from a live stream of short-lived AWS SSO proofs (one per line, <= 60 s each).

    The operator's own machine signs a fresh proof every ~20 s and pipes it in. Each new proof is
    verified once against STS; the newest valid one serves until it expires. When the stream stops,
    `__call__` fails and the drill sends no further PATCH: a human must stay present for the run.
    """

    def __init__(self, verify, lines, *, now=lambda: datetime.now().timestamp()):
        self.verify, self.lines, self.now = verify, lines, now
        self.current = None

    def __call__(self):
        token = self.lines()
        if token:
            actor = self.verify(token)
            if actor is not None:
                self.current = actor
        if self.current is None or not self.current.issued_at <= self.now() < self.current.expires_at:
            raise EdgeError("operator_proof_unavailable")
        return self.current


def _verifier(host_config):
    """The approval host's own human registry (host.json `operator_verifier`), as approval_server builds it."""
    from bravo_platform_events.operator_identity import OPERATOR_SSO_AUDIENCE, OperatorSsoVerifier
    from bravo_platform_events.registration_sso import SsoRegistrationVerifier

    people = json.loads(Path(host_config).read_text())["operator_verifier"]
    people = people if isinstance(people, list) else [people]
    return OperatorSsoVerifier([SsoRegistrationVerifier(**(o | {"audience": OPERATOR_SSO_AUDIENCE})) for o in people])


def _latest_line(stream):
    """Newest non-empty line seen on `stream` so far (read in the background); None when none is new."""
    box, lock = {"line": None}, threading.Lock()

    def pump():
        for line in stream:
            if line.strip():
                with lock:
                    box["line"] = line.strip()

    threading.Thread(target=pump, daemon=True).start()

    def take():
        with lock:
            line, box["line"] = box["line"], None
        return line
    return take


def load_drill(store, config, *, clock=_now, principal=None):
    required = {"version", "plan_id", "run_id", "approval_id", "profile", "lb_binding", "journal",
                "token_file", "operator_host"}
    if type(config) is not dict or set(config) != required or config["version"] != 1:
        raise ValueError("invalid_drill_configuration")
    journal, token_file = Path(config["journal"]), Path(config["token_file"])
    if not journal.is_absolute() or not journal.parent.is_dir() or not token_file.is_absolute():
        raise ValueError("persistent_journal_mount_required")
    spec = drill_spec(plan_id=config["plan_id"], run_id=config["run_id"], profile=config["profile"],
                      lb_binding=config["lb_binding"])
    binding = Binding(**config["lb_binding"])
    authority = DrillAuthority(store, spec=spec, approval_id=config["approval_id"], clock=clock)
    provider = Cloudflare(binding, token=lambda: token_file.read_text())
    host = DrillHost(store, provider, journal, authority=authority, clock=clock)
    return ShiftRuntime(host, principal=principal, plan_id=config["plan_id"], run_id=config["run_id"]), authority


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", required=True, help="protected drill JSON on the approval host")
    parser.add_argument("action", choices=("digest", "register", "sequence", "watch", "restore"))
    args = parser.parse_args(argv)
    config = json.loads(Path(args.config).read_text())
    emit = lambda value: print(json.dumps(value, separators=(",", ":")), flush=True)  # noqa: E731
    if args.action == "digest":  # what the approver approves; no DB, token or proof needed
        spec = drill_spec(plan_id=config["plan_id"], run_id=config["run_id"], profile=config["profile"],
                          lb_binding=config["lb_binding"])
        emit(dict(infra_plan_id=config["run_id"], plan_digest=spec_digest(spec)))
        return
    runtime = None
    try:
        from bravo_platform_events.postgres import PostgresPlanStore
        from bravo_platform_events.runtime_probe import connection_checks
        from bravo_platform_events.window_server import runtime_connection

        connection = runtime_connection()
        connection_checks(connection)
        principal = ProofStream(_verifier(config["operator_host"]).verify, _latest_line(sys.stdin))
        runtime, authority = load_drill(PostgresPlanStore(connection), config, principal=principal)
        time.sleep(1)  # let the first proof arrive before the first authenticated call
        if args.action == "register":
            emit(authority.register(principal(), runtime.plan_id, run_id=config["run_id"],
                                    profile=config["profile"], binding=runtime.host.provider.binding))
            return
        if args.action == "restore":  # the same operation id sequence uses for its own 0%
            emit(runtime.command("restore", _key("runtime-stage", [config["run_id"], 0])))
            return
        stop = Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        result = getattr(runtime, args.action)(stop=stop, emit=emit)
        emit(dict(result=result))
        if result is None or result.get("status") == "STOPPED":
            raise SystemExit(2)
    except SystemExit:
        raise
    except Exception:
        # Never print a proof, token, DSN or provider body.
        raise SystemExit("edge_drill_unavailable") from None
    finally:
        if runtime is not None:
            runtime.host.provider.close()


if __name__ == "__main__":
    main()
