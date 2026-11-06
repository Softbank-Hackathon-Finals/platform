"""T10 transaction host for T17, using T36's stored window and canonical judge.

The mandatory authority reads current human grants, gate provenance, revocation
and security holds under this same plan lock. No HTTP proof or default authority
is installed. The host never advances a plan into SHIFT or changes a DB writer.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime

from bravo_observability.contract import validate
from bravo_observability.sli import judge

from .control import Command, Controller, _json, _now, sli_rollback
from .provider import EdgeError


def _time(value):
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise EdgeError("timezone_required")
    return result


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _key(kind, value):
    return "edge-host-" + kind + "-" + _digest(value)


def _get(c, plan_id, key):
    row = c.execute("SELECT request_json,receipt_json FROM plan_commands "
                    "WHERE plan_id=? AND command_key=?", (plan_id, key)).fetchone()
    return (json.loads(row["request_json"]), json.loads(row["receipt_json"])) if row else None


def _put(c, plan_id, key, request, receipt):
    old = _get(c, plan_id, key)
    if old is not None:
        if old != (request, receipt):
            raise EdgeError("host_record_conflict")
        return
    c.execute("INSERT INTO plan_commands VALUES (?,?,?,?)",
              (plan_id, key, _json(request), _json(receipt)))


@dataclass(frozen=True)
class CurrentShift:
    """Returned only by the trusted host's current-authority reader.

    revision binds current grants/gate/holds; profile is the server-selected T36
    policy for this run. It is separate from the plan's T5 evaluation profile.
    """

    approval_id: str
    run_id: str
    revision: str
    profile: dict
    lb_binding: dict


class ShiftHost:
    def __init__(self, store, provider, journal, *, authority, clock=_now):
        if not callable(authority):
            raise ValueError("current_shift_authority_required")
        self.store, self.provider, self.journal = store, provider, journal
        self.authority, self.clock = authority, clock

    def _current(self, c, principal, plan_id):
        row = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None or row["state"] != "SHIFT":
            raise EdgeError("current_shift_required")
        plan = dict(row)
        current = self.authority(c, principal, plan)
        if not isinstance(current, CurrentShift):
            raise EdgeError("current_shift_authority_required")
        current = CurrentShift(**json.loads(_json(asdict(current))))
        if current.lb_binding != asdict(self.provider.binding):
            raise EdgeError("registered_lb_binding_mismatch")
        if any(not isinstance(v, str) or not v or len(v) > 256 for v in
               (current.approval_id, current.run_id, current.revision)):
            raise EdgeError("invalid_shift_authority")
        # WorkflowRecovery owns this checkpoint. A state label alone is not proof.
        checkpoint = _get(c, plan_id, f"workflow:{plan['version'] - 1}")
        if checkpoint is None:
            raise EdgeError("shift_checkpoint_required")
        proof = checkpoint[1]
        spec = json.loads(plan["spec_json"])
        approval = proof.get("result", {})
        if (proof.get("plan_id") != plan_id or proof.get("version") != plan["version"] - 1
                or proof.get("plan_digest") != spec["plan_digest"]
                or proof.get("target_environment_id") != spec["target_environment_id"]
                or approval.get("approval_id") != current.approval_id
                or approval.get("approved") is not True
                or _time(approval["expires_at"]) <= _time(self.clock())):
            raise EdgeError("shift_approval_unavailable")
        # The upstream validator owns profile syntax/statistics, including limits.
        judge(current.profile, None, plan_id=plan_id, state="SHIFT", weight_target=0,
              observe_from=self.clock(), decided_at=self.clock(), prior_unknown=0)
        return plan, current, proof

    def _controller(self, authorize):
        return Controller(self.provider, self.journal, authorize=authorize, clock=self.clock)

    @staticmethod
    def _latest_window(c, plan_id):
        row = c.execute("SELECT document_json FROM sli_windows WHERE plan_id=? "
                        "ORDER BY end_us DESC LIMIT 1", (plan_id,)).fetchone()
        return json.loads(row["document_json"]) if row else None

    @staticmethod
    def _stage(c, plan_id, current, last):
        if (last is None or last["plan_id"] != plan_id or last["run_id"] != current.run_id
                or last["status"] != "CONFIRMED" or last["weight_target"] == 0):
            raise EdgeError("confirmed_current_stage_required")
        saved = _get(c, plan_id, _key("receipt", last["operation_id"]))
        intent = _get(c, plan_id, _key("intent", last["operation_id"]))
        immutable = ("plan_id", "run_id", "operation_id", "revision", "weight_target", "confirmed_at")
        if (saved is None or intent is None
                or any(saved[1][field] != last[field] for field in immutable)
                or intent[1]["authority"] != asdict(current)):
            raise EdgeError("current_stage_binding_changed")
        return last

    def _judge(self, c, plan_id, current, stage):
        window = self._latest_window(c, plan_id)
        now = self.clock()
        previous = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? "
            "AND command_key LIKE ? ORDER BY command_key DESC LIMIT 1",
            (plan_id, f"edge-host-judgement-{stage['revision']:013}-%")).fetchone()
        previous = json.loads(previous["receipt_json"]) if previous else None
        # Polling the same sample does not accumulate UNKNOWNs. Missing/stale
        # observations may count once per policy window, using server time only.
        bucket = int(_time(now).timestamp()) // current.profile["window_seconds"]
        observation = _digest(dict(window=window, bucket=bucket))
        prior = 0
        if previous:
            decision = previous["judgement"]
            if previous["observation"] == observation:
                prior = decision["prior_unknown"]
            elif decision["verdict"] == "UNKNOWN":
                prior = decision["prior_unknown"] + 1
        decision = judge(current.profile, window, plan_id=plan_id, state="SHIFT",
            weight_target=stage["weight_target"], observe_from=stage["confirmed_at"],
            decided_at=now, prior_unknown=prior)
        validate("SliJudgement", decision)
        return decision, observation, previous

    def _record_judgement(self, c, plan_id, stage, decision, observation, previous):
        if previous and previous["observation"] == observation:
            # Re-evaluate freshness, but never overwrite the immutable first report.
            return
        sequence = (previous["sequence"] if previous else 0) + 1
        _put(c, plan_id,
             f"edge-host-judgement-{stage['revision']:013}-{sequence:013}", {},
             dict(sequence=sequence, observation=observation, judgement=decision))

    def _prepare(self, principal, plan_id, operation_id, action):
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 256:
            raise ValueError("invalid_operation_id")
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            plan, current, proof = self._current(c, principal, plan_id)
            key = _key("intent", operation_id)
            request = dict(action=action, run_id=current.run_id)
            old = _get(c, plan_id, key)
            if old:
                if old[0] != request:
                    raise EdgeError("host_idempotency_conflict")
                return old[1]
            last = self._controller(lambda _: False).latest_receipt()
            if (action == "observe" and last and last["reason"] == "SLI_ABORT"
                    and (last["plan_id"], last["run_id"]) == (plan_id, current.run_id)):
                pending = _get(c, plan_id, _key("intent", last["operation_id"]))
                if pending is None:
                    raise EdgeError("abort_intent_missing")
                return pending[1]
            revision = last["revision"] if last else 0
            weight = last["weight_target"] if last else 0
            decision = None
            if action == "start":
                if last and (last["status"] != "CONFIRMED" or weight != 0):
                    raise EdgeError("origin_baseline_required")
                target = 10
            else:
                stage = self._stage(c, plan_id, current, last)
                if action in {"advance", "observe"}:
                    decision, observation, previous = self._judge(c, plan_id, current, stage)
                    self._record_judgement(c, plan_id, stage, decision, observation, previous)
                    if action == "observe":
                        abort = sli_rollback(decision, plan_id=plan_id, run_id=current.run_id,
                            revision=revision, weight=weight, observe_from=stage["confirmed_at"])
                        if abort is None:
                            return dict(judgement=decision, command=None)
                        operation_id = abort.operation_id
                        key = _key("intent", operation_id)
                        old = _get(c, plan_id, key)
                        if old:
                            return old[1]
                        target = 0
                    else:
                        if decision["action"] != "ADVANCE_ALLOWED" or weight not in (10, 50):
                            raise EdgeError("advance_not_allowed")
                        target = {10: 50, 50: 100}[weight]
                else:
                    target = 0
            command = Command(plan_id, current.run_id, operation_id, revision, weight, target,
                              "SLI_ABORT" if action == "observe" else "SHIFT")
            intent = dict(command=asdict(command), authority=asdict(current),
                          plan_version=plan["version"], checkpoint_digest=_digest(proof),
                          judgement=decision)
            _put(c, plan_id, key, request, intent)
        # Intent commits before the external call. A retry uses this same command.
        return intent

    def _dispatch(self, principal, plan_id, intent):
        command = Command(**intent["command"])
        with self.store._transaction(write=True, plan_id=plan_id) as c:
            last = self._controller(lambda _: False).latest_receipt()

            def authorize(actual):
                plan, current, proof = self._current(c, principal, plan_id)
                if (actual != command or plan["version"] != intent["plan_version"]
                        or asdict(current) != intent["authority"]
                        or _digest(proof) != intent["checkpoint_digest"]):
                    return False
                # A delayed advance cannot spend an old PASS after its window ages.
                if (command.weight_target in (50, 100)
                        and (last is None or last["operation_id"] != command.operation_id)):
                    stage = self._stage(c, plan_id, current, last)
                    decision, _, _ = self._judge(c, plan_id, current, stage)
                    if decision["action"] != "ADVANCE_ALLOWED":
                        return False
                return True
            controller = self._controller(authorize)
            receipt = controller.execute(command)
            # Keep every readback, including uncertainty; do not replace an earlier
            # journal receipt with a different value at the same immutable key.
            key = _key("readback", dict(operation=command.operation_id, receipt=receipt))
            _put(c, plan_id, key, {}, receipt)
            stable_key = _key("receipt", command.operation_id)
            if receipt["status"] == "CONFIRMED" and _get(c, plan_id, stable_key) is None:
                _put(c, plan_id, stable_key, {}, receipt)
            return receipt

    def start(self, principal, plan_id, operation_id):
        return self._dispatch(principal, plan_id,
                              self._prepare(principal, plan_id, operation_id, "start"))

    def advance(self, principal, plan_id, operation_id):
        return self._dispatch(principal, plan_id,
                              self._prepare(principal, plan_id, operation_id, "advance"))

    def restore(self, principal, plan_id, operation_id):
        return self._dispatch(principal, plan_id,
                              self._prepare(principal, plan_id, operation_id, "restore"))

    def observe(self, principal, plan_id):
        # The rollback operation ID comes from the exact canonical judgement.
        intent = self._prepare(principal, plan_id, "observe-current", "observe")
        receipt = self._dispatch(principal, plan_id, intent) if intent["command"] else None
        return dict(judgement=intent["judgement"], rollback=receipt)
