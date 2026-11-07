#!/usr/bin/env python3
"""Happy-path run driver (T28): the operator's side of one integrated run, recorded.

The plan's own progress belongs to T10 (8.2 state machine, ADR-0031 workflow host). This
driver never moves plan state and never approves. It walks the design 7.6 sequence and, per
step, either runs an existing operator tool, waits until the plan reaches a state, or stops
for a human decision (change approval, cutover approval, failback approval) that is made in
the console and then confirmed here with `confirm`.

Every run is one append-only JSONL record: a header (develop SHA, GitOps image pins,
profile real/local/mock, plan) and one line per step outcome, as "develop 머지 방식" 6.3
asks. `run` resumes after the last finished step. A step whose outcome is unknown stops the
run and is never repeated automatically; a person reads the real state and records it with
`resolve`. Steps without a wired command or reader stop as `unwired` (fail closed).
Exit codes: 0 all steps done, 10 waiting for a human or a later plan state, 1 failed,
3 unknown or unwired.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DONE, WAITING, FAILED, UNKNOWN = 0, 10, 1, 3
# design 8.2, happy path order then the S4 branch
ORDER = ("COLLECT", "ANALYZE", "PROPOSE", "REHEARSE", "GATE", "AWAIT_APPROVAL", "SHIFT", "CUTOVER",
         "WATCH", "FAILBACK", "DONE_ROLLED_BACK")
STOP = {"SAFE_HOLD", "NEEDS_HUMAN"}
WORKLOADS = "infra/envs/target-workloads"
INITIAL = ("uv", "run", "--directory", "services/platform-events", "python", "../../operations/initial-proposal/run.py")
SECURITY = ("python3", "operations/security-program/run.py")
# Security Program documents each prepare command needs (operator_ports: change needs patch/preflight,
# cutover needs patch/postflight and migration/preflight).
SECURITY_INPUTS = {"prepare-change": ("patch_preflight",),
                   "prepare-cutover": ("patch_postflight", "migration_preflight")}
# After the budget runs out, one bounded read-only plan read records where the real plan stopped.
BUDGET_READ_SECONDS = 10
PLAN_STATE = ("kubectl", "--context", "{context}", "-n", "platform-state", "exec", "-i", "deploy/t10-workflow-host",
              "-c", "host", "--", "python", "-m", "bravo_platform_events.plan_state")


@dataclass(frozen=True)
class Step:
    id: str
    scene: str
    kind: str  # operator | observe | human
    owner: str
    what: str
    command: tuple[str, ...] | None = None  # operator; None = unwired
    reach: str | None = None  # observe: the plan state that ends the wait


STEPS = (
    Step("preflight", "S0", "operator", "T28", "목적지 시작 상태 확인",
         ("python3", f"{WORKLOADS}/demo_reset.py", "check", "--kubeconfig", "{kubeconfig}",
          "--fault-record", "{fault_record}")),
    Step("s1-analyzed", "S1", "observe", "T10", "수집·분석 완료, 게이트 1차 BLOCK 기록", reach="PROPOSE"),
    Step("s2-ai-start", "S2", "operator", "T10/T38", "AI 제안 Job 시작(T10 접수 → Job 1회)", INITIAL + ("start",)),
    Step("s2-ai-collect", "S2", "operator", "T10/T38", "AI 제안 영수증 → T10 저장, T15에 runtime·digest 전달",
         INITIAL + ("collect",)),
    Step("s2-candidate-import", "S2", "human", "T22/T31",
         "후보 이미지를 목적지 containerd에 같은 digest로 넣기(export_target_image.py → import → digest 대조)"),
    Step("s2-proposed", "S2", "observe", "T10/T38", "후보 발행 확인, 리허설 진입", reach="REHEARSE"),
    Step("s2-security-prepare", "S2", "operator", "T15/T31",
         "현재 patch/preflight 등록과 원격 Security Review PASS",
         SECURITY + ("prepare-change", "--context", "{platform_context}", "--plan-id", "{plan_id}",
                     "--patch-preflight", "{patch_preflight}", "--budget-seconds", "45")),
    Step("s2-change-approval", "S2", "human", "승인자", "콘솔에서 diff·digest 결속 변경 승인"),
    Step("s2-open-writes", "S2", "operator", "T30", "T31 APPLIED 뒤 목적지 쓰기 열기",
         ("python3", f"{WORKLOADS}/open_after_apply.py", "--application-id", "{application_id}",
          "--receipts", "{receipts}", "--kubeconfig", "{kubeconfig}")),
    Step("s2-gate-pass", "S2", "observe", "T19", "게이트 재대조 PASS", reach="AWAIT_APPROVAL"),
    # Measurements and attachment copying must exist before either approval can
    # release a real transition. The driver still delegates the owner tools.
    Step("s3-load", "S3", "human", "T18", "부하 예열 50 rps · 30초 → 본 측정 시작 (services/loadgen)"),
    Step("s3-attachment-copy", "S3", "human", "T8",
         "업로드 중지 → 첨부 복사 Job(attachment_job.py job) → 판정 SUCCEEDED"),
    Step("s3-shift-approval", "S3", "human", "승인자",
         "현재 T13 캡처·서명 확인 후 SHIFT 승인(approval_kind: shift)"),
    # The cutover authority requires current patch/postflight and migration/preflight registrations and a
    # remote Security Review PASS (operator_ports); without them the cutover approval holds (#939 review 2).
    Step("s3-security-prepare", "S3", "operator", "T15/T31",
         "컷오버용 patch/postflight · migration/preflight 등록과 원격 Security Review PASS",
         SECURITY + ("prepare-cutover", "--context", "{platform_context}", "--plan-id", "{plan_id}",
                     "--patch-postflight", "{patch_postflight}", "--migration-preflight", "{migration_preflight}",
                     "--budget-seconds", "45")),
    Step("s3-cutover-approval", "S3", "human", "승인자",
         "최신 GATE·T13 확인 후 콘솔에서 컷오버 승인·실행 요청(SHIFT 상태)"),
    Step("s3-shifted", "S3", "observe", "T17", "가중치 10 → 50 → 100%", reach="CUTOVER"),
    Step("s3-cutover-done", "S3", "observe", "T13", "컷오버 영수증, 무결성 일치", reach="WATCH"),
    Step("s3-reverse-replication", "S3", "human", "T8", "역방향 복제 도착 확인(resume_replication.py --direction reverse)"),
    Step("s4-fault-inject", "S4", "operator", "T28", "목적지 장애 주입",
         ("python3", f"{WORKLOADS}/demo_fault.py", "inject", "--record", "{fault_record}",
          "--kubeconfig", "{kubeconfig}")),
    Step("s4-alert-seen", "S4", "human", "운영자", "WATCH 판정·되돌리기 제안 알림 확인"),
    Step("s4-fault-clear", "S4", "operator", "T28", "장애 해제 (FAILBACK 승인 전)",
         ("python3", f"{WORKLOADS}/demo_fault.py", "clear", "--record", "{fault_record}",
          "--kubeconfig", "{kubeconfig}")),
    Step("s4-failback-approval", "S4", "human", "승인자", "콘솔에서 되돌리기 승인"),
    Step("s4-rolled-back", "S4", "observe", "T20", "출발지 복귀, 데이터째 되돌리기", reach="DONE_ROLLED_BACK"),
    Step("wrap-map", "S5", "human", "T27", "잔존 의존성 지도(bravo_exit_drill render/verdict)"),
)


def selected_steps(scenario):
    if scenario == "recovery":
        return STEPS
    if scenario != "migration":
        raise ValueError("unknown rehearsal scenario")
    end = next(i for i, step in enumerate(STEPS) if step.id == "s3-reverse-replication")
    return STEPS[:end + 1]


def now():
    return datetime.now(UTC).isoformat()


def read_record(path):
    if not path.exists():
        return None, {}
    lines = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    outcomes = {}
    for line in lines[1:]:
        outcomes[line["step"]] = line
    return lines[0], outcomes


def append(path, value):
    with path.open("a") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()


def pins():
    out = {}
    for name in ("registration", "product-reader", "rehearsal-host"):
        image = json.loads((ROOT / f"infra/gitops/environments/development/{name}-image.json").read_text())
        out[name] = image["image"]["digest"]
    return out


def header(args, git=subprocess.run, wall_clock=time.time):
    sha = git(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"kind": "header", "plan_id": args.plan_id, "profile": args.profile,
            "scenario": getattr(args, "scenario", "recovery"), "develop_sha": sha,
            "gitops_pins": pins(), "started_at": now(),
            "demo_budget_seconds": getattr(args, "demo_budget", 270),
            "budget_started_epoch": wall_clock()}


def budget_remaining(head, wall_clock=time.time):
    seconds = head.get("demo_budget_seconds")
    started = head.get("budget_started_epoch")
    if type(seconds) is not int or not 0 < seconds <= 270 or not isinstance(started, (int, float)):
        raise ValueError("invalid demo budget record")
    return started + seconds - wall_clock()


def plan_state_from(command):
    """A configured read-only command that prints {"state": ...} for the plan; None = unwired."""
    if not command:
        return None

    def read(plan_id, timeout_seconds=60):
        result = subprocess.run([*command, plan_id], capture_output=True, text=True,
                                timeout=timeout_seconds, check=False)
        if result.returncode:
            raise RuntimeError("plan read failed")
        return json.loads(result.stdout.splitlines()[-1])["state"]
    read.budget_aware = True
    return read


def read_plan_state(read_state, plan_id, timeout_seconds=None):
    if timeout_seconds is not None and getattr(read_state, "budget_aware", False):
        if timeout_seconds <= 0:
            raise subprocess.TimeoutExpired("plan-state", timeout_seconds)
        return read_state(plan_id, timeout_seconds=timeout_seconds)
    return read_state(plan_id)


def budget_snapshot(read_state, plan_id):
    """One bounded read-only plan read once the budget is gone: the record shows where the real plan stopped.
    Nothing is run or retried; an unread state is reported as such, never assumed."""
    if read_state is None:
        return {"plan_state_at_budget": "unread"}
    try:
        return {"plan_state_at_budget": read_plan_state(read_state, plan_id, BUDGET_READ_SECONDS)}
    except Exception:  # noqa: BLE001 - an unconfirmed read stays unconfirmed
        return {"plan_state_at_budget": "unconfirmed"}


def reached(state, goal):
    if state == "DONE":
        return goal in ORDER[:ORDER.index("WATCH") + 1]
    return state in ORDER and ORDER.index(state) >= ORDER.index(goal)


def run_step(step, args, read_state, run, sleep, clock, remaining_seconds=None):
    if step.kind == "human":
        return WAITING, {"reason": "awaiting_human", "what": step.what}
    if step.kind == "observe":
        if read_state is None:
            return UNKNOWN, {"reason": "plan_reader_unwired"}
        budget_limited = remaining_seconds is not None and remaining_seconds < args.wait
        wait = args.wait if remaining_seconds is None else min(args.wait, max(0, remaining_seconds))
        deadline = clock() + wait
        state = None
        while True:
            if state is not None and clock() >= deadline:
                if budget_limited:
                    return UNKNOWN, {"reason": "demo_budget_exhausted_during_step", "state": state}
                return WAITING, {"reason": "plan_not_yet", "state": state, "reach": step.reach}
            try:
                read_timeout = 60 if remaining_seconds is None else remaining_seconds
                if wait:
                    read_timeout = min(read_timeout, deadline - clock())
                state = read_plan_state(read_state, args.plan_id, read_timeout)
            except Exception:  # noqa: BLE001 - the read itself is not confirmed
                return UNKNOWN, {"reason": "plan_read_unconfirmed"}
            if state in STOP:
                return FAILED, {"reason": "plan_stopped", "state": state}
            if reached(state, step.reach):
                return DONE, {"state": state}
            if clock() >= deadline:
                if budget_limited:
                    return UNKNOWN, {"reason": "demo_budget_exhausted_during_step", "state": state}
                return WAITING, {"reason": "plan_not_yet", "state": state, "reach": step.reach}
            sleep(min(args.interval, max(0, deadline - clock())))
    if step.command is None:
        return UNKNOWN, {"reason": "unwired", "owner": step.owner}
    if step.command[:len(SECURITY)] == SECURITY:
        needed = ("platform_context", *SECURITY_INPUTS[step.command[len(SECURITY)]])
        missing = [name for name in needed if not getattr(args, name, None)]
        if missing:
            return UNKNOWN, {"reason": "security_program_unwired", "owner": step.owner, "missing": missing}
    values = {"kubeconfig": args.kubeconfig or "", "fault_record": args.fault_record,
              "application_id": args.application_id or "", "receipts": args.receipts or "",
              "platform_context": args.platform_context or "", "plan_id": args.plan_id,
              "patch_preflight": getattr(args, "patch_preflight", None) or "",
              "patch_postflight": getattr(args, "patch_postflight", None) or "",
              "migration_preflight": getattr(args, "migration_preflight", None) or ""}
    argv = [part.format(**values) for part in step.command]
    if step.command[:len(INITIAL)] == INITIAL:
        if not getattr(args, "initial_args", None):
            return UNKNOWN, {"reason": "initial_args_unwired", "owner": step.owner}
        argv += list(args.initial_args)
    try:
        timeout = args.step_timeout
        budget_limited = False
        if remaining_seconds is not None:
            budget_limited = remaining_seconds < timeout
            timeout = min(timeout, remaining_seconds)
        result = run(argv, cwd=ROOT, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        reason = "demo_budget_exhausted_during_step" if budget_limited else "step_timeout"
        return UNKNOWN, {"reason": reason}
    except Exception:  # noqa: BLE001 - a lost result is unknown, never retried
        return UNKNOWN, {"reason": "command_unconfirmed"}
    last = result.stdout.strip().splitlines()[-1:] or [""]
    try:
        output = json.loads(last[0])
    except ValueError:
        output = {"stdout_tail": last[0][:300]}
    # 10 = still running (e.g. the AI proposal Job): wait and run the same idempotent step again later.
    code = {0: DONE, 3: UNKNOWN, 10: WAITING}.get(result.returncode, FAILED)
    return code, {"exit_code": result.returncode, "output": output}


def cmd_run(args, *, run=subprocess.run, sleep=time.sleep, clock=time.monotonic, wall_clock=time.time,
            read_state=None, git=subprocess.run):
    path = Path(args.record)
    head, outcomes = read_record(path)
    if head is None:
        head = header(args, git, wall_clock)
        append(path, head)
    elif head["plan_id"] != args.plan_id:
        print(json.dumps({"result": "input_error", "reason": "record belongs to another plan"}))
        return 2
    if (head.get("profile") != args.profile
            or head.get("scenario", "recovery") != getattr(args, "scenario", "recovery")
            or head.get("demo_budget_seconds") != getattr(args, "demo_budget", 270)):
        print(json.dumps({"result": "input_error", "reason": "record profile, scenario, or budget differs"}))
        return 2
    command = args.plan_state_command or (
        [part.format(context=args.platform_context) for part in PLAN_STATE] if getattr(args, "platform_context", None)
        else None)
    read_state = read_state or plan_state_from(command)
    selected = selected_steps(getattr(args, "scenario", "recovery"))
    for step in selected:
        seen = outcomes.get(step.id, {})
        if seen.get("status") in ("done", "human_confirmed"):
            continue
        if seen.get("status") in ("unknown", "failed"):
            # Neither is repeated automatically: read the real state, then `resolve`.
            print(json.dumps({"result": seen["status"], "step": step.id, "reason": "resolve the real state first"}))
            return UNKNOWN if seen["status"] == "unknown" else FAILED
        try:
            remaining = budget_remaining(head, wall_clock)
        except ValueError:
            print(json.dumps({"result": "input_error", "reason": "invalid_demo_budget_record"}))
            return 2
        if remaining is not None and remaining <= 0:
            detail = {"reason": "demo_budget_exceeded", "budget_seconds": head["demo_budget_seconds"],
                      **budget_snapshot(read_state, args.plan_id)}
            append(path, {"step": step.id, "scene": step.scene, "status": "failed", "at": now(), **detail})
            print(json.dumps({"result": "failed", "step": step.id, **detail}))
            return FAILED
        code, detail = run_step(step, args, read_state, run, sleep, clock, remaining)
        if remaining is not None:
            detail["budget_remaining_seconds"] = max(0, round(budget_remaining(head, wall_clock), 3))
        status = {DONE: "done", WAITING: "waiting", FAILED: "failed", UNKNOWN: "unknown"}[code]
        if status != "waiting":
            append(path, {"step": step.id, "scene": step.scene, "status": status, "at": now(), **detail})
        print(json.dumps({"result": status, "step": step.id, "owner": step.owner, "what": step.what, **detail},
                         ensure_ascii=False))
        if code != DONE:
            return code
    if (remaining := budget_remaining(head, wall_clock)) is not None and remaining <= 0:
        # Every step ran but the visible demo took too long: still record the real plan state once.
        detail = {"reason": "demo_budget_exceeded", "budget_seconds": head["demo_budget_seconds"],
                  **budget_snapshot(read_state, args.plan_id)}
        append(path, {"step": "demo-budget", "scene": "-", "status": "failed", "at": now(), **detail})
        print(json.dumps({"result": "failed", **detail}))
        return FAILED
    try:
        final_timeout = None if remaining is None else max(0, remaining)
        state = read_plan_state(read_state, args.plan_id, final_timeout)
    except Exception:  # noqa: BLE001 - the final read is unconfirmed, never assumed
        print(json.dumps({"result": "unknown", "reason": "final_plan_read_unconfirmed"}))
        return UNKNOWN
    if getattr(args, "scenario", "recovery") == "migration":
        if state not in {"WATCH", "DONE"}:
            print(json.dumps({"result": "failed", "reason": "forward_migration_not_current", "state": state}))
            return FAILED
    elif state != "DONE_ROLLED_BACK":
        print(json.dumps({"result": "failed", "reason": "failback_not_current", "state": state}))
        return FAILED
    elapsed = round(head["demo_budget_seconds"] - budget_remaining(head, wall_clock), 3)
    print(json.dumps({"result": "complete", "record": str(path),
                      "scenario": getattr(args, "scenario", "recovery"), "final_state": state,
                      "elapsed_seconds": elapsed, "budget_seconds": head["demo_budget_seconds"]}))
    return DONE


def cmd_mark(args, status):
    path = Path(args.record)
    head, outcomes = read_record(path)
    ids = [step.id for step in selected_steps(head.get("scenario", "recovery"))] if head else []
    if head is None or args.step not in ids:
        print(json.dumps({"result": "input_error"}))
        return 2
    step = STEPS[ids.index(args.step)]
    if status == "human_confirmed" and step.kind != "human":
        print(json.dumps({"result": "input_error", "reason": "only human steps are confirmed"}))
        return 2
    if status != "human_confirmed" and outcomes.get(args.step, {}).get("status") not in ("unknown", "failed"):
        print(json.dumps({"result": "input_error", "reason": "only an unknown or failed outcome is resolved"}))
        return 2
    append(path, {"step": args.step, "status": status if status == "human_confirmed" else args.status,
                  "at": now(), "by": args.by, "note": args.note})
    return DONE


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("steps", help="print the sequence")
    r = sub.add_parser("run")
    r.add_argument("--plan-id", required=True)
    r.add_argument("--record", required=True, help="append-only JSONL run record")
    r.add_argument("--profile", choices=["real", "local", "mock"], required=True)
    r.add_argument("--scenario", choices=["migration", "recovery"], default="recovery",
                   help="migration stops after verified forward movement; recovery also exercises faults/failback")
    r.add_argument("--plan-state-command", nargs="+", help="read-only command printing {\"state\": ...}")
    r.add_argument("--kubeconfig")
    r.add_argument("--platform-context", help="kubectl context of the platform cluster (default plan reader)")
    r.add_argument("--initial-args", nargs="+", help="LAST option: every argument after it goes to "
                   "operations/initial-proposal/run.py as is (--initial-args --from initial.json)")
    r.add_argument("--application-id")
    r.add_argument("--receipts")
    r.add_argument("--patch-preflight", help="current patch/preflight Security Program document")
    r.add_argument("--patch-postflight", help="current patch/postflight Security Program document (cutover)")
    r.add_argument("--migration-preflight", help="current migration/preflight Security Program document (cutover)")
    r.add_argument("--fault-record", default="demo-fault.json")
    r.add_argument("--wait", type=int, default=0, help="seconds to wait for a plan state")
    r.add_argument("--interval", type=int, default=10)
    r.add_argument("--step-timeout", type=int, default=900)
    r.add_argument("--demo-budget", type=int, default=270,
                   help="whole visible demo budget in seconds (maximum 270)")
    for name, help_text in (("confirm", "record a human decision made in the console"),
                            ("resolve", "record the read-back state of an unknown step")):
        m = sub.add_parser(name, help=help_text)
        m.add_argument("--record", required=True)
        m.add_argument("--step", required=True)
        m.add_argument("--by", required=True)
        m.add_argument("--note", default="")
        if name == "resolve":
            m.add_argument("--status", choices=["done", "failed"], required=True)
    argv = list(sys.argv[1:] if argv is None else argv)
    # run.py arguments start with "--", which argparse would read as this runner's options
    # (kth4778 11/7): everything after a bare --initial-args is passed through untouched.
    initial = None
    if "--initial-args" in argv:
        at = argv.index("--initial-args")
        argv, initial = argv[:at], argv[at + 1:]
        if not initial:
            p.error("--initial-args needs run.py arguments")
    args = p.parse_args(argv)
    if args.command == "run" and not 0 < args.demo_budget <= 270:
        p.error("--demo-budget must be between 1 and 270 seconds")
    if initial is not None:
        args.initial_args = initial
    if args.command == "steps":
        for step in STEPS:
            wired = step.kind != "operator" or step.command is not None
            print(f"{step.scene} {step.id:24} {step.kind:8} {step.owner:8} {'' if wired else '[unwired] '}{step.what}")
        return DONE
    if args.command == "run":
        return cmd_run(args)
    return cmd_mark(args, "human_confirmed" if args.command == "confirm" else "resolved")


if __name__ == "__main__":
    sys.exit(main())
