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
    Step("s2-change-approval", "S2", "human", "승인자", "콘솔에서 diff·digest 결속 변경 승인"),
    Step("s2-open-writes", "S2", "operator", "T30", "T31 APPLIED 뒤 목적지 쓰기 열기",
         ("python3", f"{WORKLOADS}/open_after_apply.py", "--application-id", "{application_id}",
          "--receipts", "{receipts}", "--kubeconfig", "{kubeconfig}")),
    Step("s2-gate-pass", "S2", "observe", "T19", "게이트 재대조 PASS", reach="AWAIT_APPROVAL"),
    Step("s3-shift-approval", "S3", "human", "승인자",
         "SHIFT 승인: POST /v1/plans/{plan}/approvals {approval_kind: shift, expected_version, expires_at}"),
    Step("s3-cutover-approval", "S3", "human", "승인자", "콘솔에서 컷오버 승인(SHIFT 상태에서)"),
    # Owner-run live tools with per-run inputs (docs/operations/t28-demo-runbook.md); confirmed here.
    Step("s3-load", "S3", "human", "T18", "부하 예열 50 rps · 30초 → 본 측정 시작 (services/loadgen)"),
    Step("s3-attachment-copy", "S3", "human", "T8",
         "업로드 중지 → 첨부 복사 Job(attachment_job.py job) → 판정 SUCCEEDED"),
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
    for name in ("registration", "product-reader"):
        image = json.loads((ROOT / f"infra/gitops/environments/development/{name}-image.json").read_text())
        out[name] = image["image"]["digest"]
    return out


def header(args, git=subprocess.run):
    sha = git(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return {"kind": "header", "plan_id": args.plan_id, "profile": args.profile, "develop_sha": sha,
            "gitops_pins": pins(), "started_at": now()}


def plan_state_from(command):
    """A configured read-only command that prints {"state": ...} for the plan; None = unwired."""
    if not command:
        return None

    def read(plan_id):
        result = subprocess.run([*command, plan_id], capture_output=True, text=True, timeout=60, check=False)
        if result.returncode:
            raise RuntimeError("plan read failed")
        return json.loads(result.stdout.splitlines()[-1])["state"]
    return read


def reached(state, goal):
    return state in ORDER and ORDER.index(state) >= ORDER.index(goal)


def run_step(step, args, read_state, run, sleep, clock):
    if step.kind == "human":
        return WAITING, {"reason": "awaiting_human", "what": step.what}
    if step.kind == "observe":
        if read_state is None:
            return UNKNOWN, {"reason": "plan_reader_unwired"}
        deadline = clock() + args.wait
        while True:
            try:
                state = read_state(args.plan_id)
            except Exception:  # noqa: BLE001 - the read itself is not confirmed
                return UNKNOWN, {"reason": "plan_read_unconfirmed"}
            if state in STOP:
                return FAILED, {"reason": "plan_stopped", "state": state}
            if reached(state, step.reach):
                return DONE, {"state": state}
            if clock() >= deadline:
                return WAITING, {"reason": "plan_not_yet", "state": state, "reach": step.reach}
            sleep(args.interval)
    if step.command is None:
        return UNKNOWN, {"reason": "unwired", "owner": step.owner}
    values = {"kubeconfig": args.kubeconfig or "", "fault_record": args.fault_record,
              "application_id": args.application_id or "", "receipts": args.receipts or ""}
    argv = [part.format(**values) for part in step.command]
    if step.command[:len(INITIAL)] == INITIAL:
        if not getattr(args, "initial_args", None):
            return UNKNOWN, {"reason": "initial_args_unwired", "owner": step.owner}
        argv += list(args.initial_args)
    try:
        result = run(argv, cwd=ROOT, capture_output=True, text=True, timeout=args.step_timeout, check=False)
    except Exception:  # noqa: BLE001 - a lost result is unknown, never retried
        return UNKNOWN, {"reason": "command_unconfirmed"}
    last = result.stdout.strip().splitlines()[-1:] or [""]
    try:
        output = json.loads(last[0])
    except ValueError:
        output = {"stdout_tail": last[0][:300]}
    code = {0: DONE, 3: UNKNOWN}.get(result.returncode, FAILED)
    return code, {"exit_code": result.returncode, "output": output}


def cmd_run(args, *, run=subprocess.run, sleep=time.sleep, clock=time.monotonic, read_state=None, git=subprocess.run):
    path = Path(args.record)
    head, outcomes = read_record(path)
    if head is None:
        head = header(args, git)
        append(path, head)
    elif head["plan_id"] != args.plan_id:
        print(json.dumps({"result": "input_error", "reason": "record belongs to another plan"}))
        return 2
    command = args.plan_state_command or (
        [part.format(context=args.platform_context) for part in PLAN_STATE] if getattr(args, "platform_context", None)
        else None)
    read_state = read_state or plan_state_from(command)
    for step in STEPS:
        seen = outcomes.get(step.id, {})
        if seen.get("status") in ("done", "human_confirmed"):
            continue
        if seen.get("status") in ("unknown", "failed"):
            # Neither is repeated automatically: read the real state, then `resolve`.
            print(json.dumps({"result": seen["status"], "step": step.id, "reason": "resolve the real state first"}))
            return UNKNOWN if seen["status"] == "unknown" else FAILED
        code, detail = run_step(step, args, read_state, run, sleep, clock)
        status = {DONE: "done", WAITING: "waiting", FAILED: "failed", UNKNOWN: "unknown"}[code]
        if status != "waiting":
            append(path, {"step": step.id, "scene": step.scene, "status": status, "at": now(), **detail})
        print(json.dumps({"result": status, "step": step.id, "owner": step.owner, "what": step.what, **detail},
                         ensure_ascii=False))
        if code != DONE:
            return code
    print(json.dumps({"result": "complete", "record": str(path)}))
    return DONE


def cmd_mark(args, status):
    path = Path(args.record)
    head, outcomes = read_record(path)
    ids = [step.id for step in STEPS]
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
    r.add_argument("--plan-state-command", nargs="+", help="read-only command printing {\"state\": ...}")
    r.add_argument("--kubeconfig")
    r.add_argument("--platform-context", help="kubectl context of the platform cluster (default plan reader)")
    r.add_argument("--initial-args", nargs="+", help="operations/initial-proposal/run.py arguments after the command")
    r.add_argument("--application-id")
    r.add_argument("--receipts")
    r.add_argument("--fault-record", default="demo-fault.json")
    r.add_argument("--wait", type=int, default=0, help="seconds to wait for a plan state")
    r.add_argument("--interval", type=int, default=10)
    r.add_argument("--step-timeout", type=int, default=900)
    for name, help_text in (("confirm", "record a human decision made in the console"),
                            ("resolve", "record the read-back state of an unknown step")):
        m = sub.add_parser(name, help=help_text)
        m.add_argument("--record", required=True)
        m.add_argument("--step", required=True)
        m.add_argument("--by", required=True)
        m.add_argument("--note", default="")
        if name == "resolve":
            m.add_argument("--status", choices=["done", "failed"], required=True)
    args = p.parse_args(argv)
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
