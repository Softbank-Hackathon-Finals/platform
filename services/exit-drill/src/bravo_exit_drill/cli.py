"""Exit Drill 명령줄. 결과는 표준 출력으로만 낸다. 적용과 재시작은 운영자의 몫이다.

종료 코드: 0 통과 · 1 실패(또는 계획 거부) · 2 입력 오류 · 3 판정 불가(UNKNOWN)
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import probe
from .cluster import SnapshotError, parse_snapshot
from .inventory_input import ContractError, parse_inventory
from .observation import ObservationError, build_observation, probe_request
from .policy import PlanRefused, build_plan, plan_is_intact
from .residual_map import MapError, build_map, render_markdown
from .spec import SpecError, parse_spec
from .verdict import exit_code, judge

DEFAULT_PROBE_TIMEOUT_S = 3
INPUT_ERRORS = (OSError, json.JSONDecodeError, UnicodeDecodeError)


class _InputError(Exception):
    """어떤 입력을 못 읽었는지만 말한다. 사용자 경로나 내용을 출력하지 않는다."""


def _load(path: str, label: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except INPUT_ERRORS as exc:
        raise _InputError(f"{label} 입력을 읽을 수 없다") from exc


def _load_or_none(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except INPUT_ERRORS:
        return None


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True))


def _plan(path: str, *, verify: bool = True) -> dict:
    plan = _load(path, "plan")
    if not isinstance(plan, dict):
        raise _InputError("plan 입력이 계획이 아니다")
    if verify and not plan_is_intact(plan):
        raise _InputError("plan 해시가 맞지 않아 쓸 수 없다")
    return plan


def _render(args) -> int:
    spec = parse_spec(_load(args.spec, "spec"))
    inventory = parse_inventory(_load(args.inventory, "inventory"), candidate=args.candidate)
    snapshot = parse_snapshot(_load(args.snapshot, "snapshot"), namespace=spec.namespace)
    try:
        _print(build_plan(spec, inventory, snapshot))
    except PlanRefused as refused:
        _print({"refused": [{"code": v.code, "subject": v.subject} for v in refused.violations]})
        return 1
    return 0


def _manifests(args) -> int:
    _print({"apiVersion": "v1", "kind": "List", "items": _plan(args.plan)["manifests"]})
    return 0


def _targets(args) -> int:
    _print({**probe_request(_plan(args.plan), args.vantage), "timeout": args.timeout})
    return 0


def _assemble(args) -> int:
    _print(build_observation(
        _plan(args.plan),
        phase=args.phase,
        pods=_load(args.pods, "pods"),
        policies_start=_load(args.policies_start, "policies-start"),
        policies_end=_load(args.policies_end, "policies-end"),
        probe_runs=_load(args.probes, "probes"),
        replication=_load(args.replication, "replication") if args.replication else [],
        cutover=_load(args.cutover, "cutover") if args.cutover else None,
        policy_applied_at=args.policy_applied_at,
        induced_restarts=_load(args.induced_restarts, "induced-restarts") if args.induced_restarts else None,
    ))
    return 0


def _check(args) -> int:
    # 관측 파일이 없거나 깨졌으면, 또는 계획을 손댔으면 입력 오류가 아니라 '믿을 증거 없음'이다.
    # 판정은 UNKNOWN이 된다.
    report = judge(_plan(args.plan, verify=False), _load_or_none(args.before), _load_or_none(args.after))
    _print(report)
    return exit_code(report)


def _map(args) -> int:
    inventory = parse_inventory(_load(args.inventory, "inventory"), candidate=args.candidate)
    report = _load(args.report, "report") if args.report else None
    result = build_map(inventory, _plan(args.plan), report)
    if args.format == "markdown":
        print(render_markdown(result))
    else:
        _print(result)
    return 0


def _probe(args) -> int:
    request = json.loads(args.request)
    _print(probe.run(request["targets"], timeout=request.get("timeout", DEFAULT_PROBE_TIMEOUT_S)))
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bravo-exit-drill", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    render = commands.add_parser("render", help="차단 계획을 만든다")
    render.add_argument("--spec", required=True)
    render.add_argument("--inventory", required=True)
    render.add_argument("--candidate", required=True)
    render.add_argument("--snapshot", required=True, help="kubectl get pods,networkpolicies -o json")
    render.set_defaults(run=_render)

    manifests = commands.add_parser("manifests", help="계획의 NetworkPolicy를 List로 출력한다")
    manifests.add_argument("--plan", required=True)
    manifests.set_defaults(run=_manifests)

    targets = commands.add_parser("targets", help="이 위치에서 잴 대상을 출력한다")
    targets.add_argument("--plan", required=True)
    targets.add_argument("--vantage", required=True)
    targets.add_argument("--timeout", type=int, default=DEFAULT_PROBE_TIMEOUT_S)
    targets.set_defaults(run=_targets)

    assemble = commands.add_parser("assemble", help="읽은 결과를 관측 파일로 묶는다")
    assemble.add_argument("--plan", required=True)
    assemble.add_argument("--phase", required=True, choices=["before", "after"])
    assemble.add_argument("--pods", required=True)
    assemble.add_argument("--policies-start", required=True)
    assemble.add_argument("--policies-end", required=True)
    assemble.add_argument("--probes", required=True)
    assemble.add_argument("--replication")
    assemble.add_argument("--cutover")
    assemble.add_argument("--policy-applied-at")
    assemble.add_argument("--induced-restarts", help="일부러 컨테이너를 다시 띄운 횟수 {Pod UID: 횟수}")
    assemble.set_defaults(run=_assemble)

    check = commands.add_parser("check", help="전후 관측을 판정한다")
    check.add_argument("--plan", required=True)
    check.add_argument("--before", required=True)
    check.add_argument("--after", required=True)
    check.set_defaults(run=_check)

    residual = commands.add_parser("map", help="잔존 의존성 지도를 출력한다")
    residual.add_argument("--inventory", required=True)
    residual.add_argument("--candidate", required=True)
    residual.add_argument("--plan", required=True)
    residual.add_argument("--report")
    residual.add_argument("--format", choices=["json", "markdown"], default="json")
    residual.set_defaults(run=_map)

    probing = commands.add_parser("probe", help="Pod 안에서 대상을 잰다")
    probing.add_argument("request", help='{"targets": [...], "timeout": 3}')
    probing.set_defaults(run=_probe)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.run(args)
    except (_InputError, ContractError, SpecError, SnapshotError, ObservationError, MapError) as exc:
        print(f"bravo-exit-drill: {exc}", file=sys.stderr)
    except Exception:  # noqa: BLE001 - 예외로 죽으면 종료 코드 1이라 FAIL로 읽힌다. 내용은 찍지 않는다.
        print("bravo-exit-drill: 입력을 처리할 수 없다", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
