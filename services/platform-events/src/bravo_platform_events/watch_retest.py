"""WATCH control retest (design S5 step 6; state table: WATCH passes on a retest PASS).

After the cutover, T14 records a target run through the same intake as the post-apply run
(gate_record_host / GateRecords.record_run). This compares the newest one that started after the
cutover's last observed update with the source run of the current GATE context, using T19's own
evaluator. It runs under the plan lock from stored records only: no network, nothing written, no
context bound. No exception is applied (the GATE assembly has none).

PASS     T19's comparison is PASS.
FAIL     a required control regressed on the target (TARGET_FAIL): the operator's FAILBACK path.
UNKNOWN  anything else: no run after the cutover yet, expired evidence, an unusable record. WATCH
         waits; record a new run. The source run's evidence must still be valid at WATCH.

ports.json: "watch": {"profile_file": "...", "retest": "bravo_platform_events.watch_retest:create"}
"""

import json
from dataclasses import replace
from datetime import UTC, datetime

from bravo_gate.contract import GateError, T5Contract, time
from bravo_gate.engine import evaluate
from bravo_gate.models import GateContext

from .cutover import latest_observation
from .gate_records import _current


def cutover_finished(c, plan, run):
    """The cutover run's last observed update: the same point WATCH observes the SLI from."""
    return time(latest_observation(c, plan["plan_id"], run.command_id)["snapshot"]["updated_at"])


def _runs(c, plan_id):
    rows = c.execute("SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ?",
                     (plan_id, "gate-run-%")).fetchall()
    return [json.loads(row["receipt_json"]) for row in rows]


def retest_reader(contract, *, clock=None, since=cutover_finished):
    """retest(c, plan, run) -> "PASS" / "FAIL" / "UNKNOWN" for workflow_stages.watch_reader."""
    clock = clock or (lambda: datetime.now(UTC))

    def retest(c, plan, run):
        try:
            gate = _current(c, plan["plan_id"])
            if gate is None:
                return "UNKNOWN"
            ctx = GateContext(**gate["context"])
            finished = since(c, plan, run)
            runs = _runs(c, plan["plan_id"])
            source = [r for r in runs if r.get("side") == "source" and r.get("digest") == ctx.source_digest]
            after = [r for r in runs if r.get("side") == "target" and "ref" in r
                     and r["document"]["environment"]["id"] == ctx.target_environment_id
                     and time(r["document"]["started_at"]) >= finished]
            if len(source) != 1 or not after:
                return "UNKNOWN"
            target = max(after, key=lambda r: time(r["document"]["started_at"]))
            retest_ctx = replace(ctx, gate_id="post-cutover-" + run.execution_id,
                                 target_ref=target["ref"], target_digest=target["digest"])
            report = evaluate(contract, retest_ctx, source[0]["document"], target["document"], now=clock())
        except (GateError, KeyError, TypeError, ValueError):
            return "UNKNOWN"
        if report["decision"] == "PASS":
            return "PASS"
        if not report["run_issues"] and any(row["reason"] == "TARGET_FAIL" for row in report["rows"]):
            return "FAIL"
        return "UNKNOWN"

    return retest


def create(_store):
    """ports.json "watch.retest" factory."""
    return retest_reader(T5Contract())
