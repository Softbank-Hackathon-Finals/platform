"""격리 시험 실행.

    uv run python -m rehearsal_runner.isolation --create \\
        --namespace rehearsal-plan1-1 --profile profiles/k3d.env --context k3d-bravo-rehearsal

--create: 템플릿으로 리허설 네임스페이스를 만들거나 갱신한 뒤 바로 시험한다 (T15가 부르는 형태).
없으면 이미 있는 네임스페이스를 시험만 한다. 리허설 네임스페이스는 지우지 않고, 시험용 Pod만 지운다.
결과는 표준 출력의 표와 --report의 JSON으로 남고, 종료 코드는 0 통과 · 1 격리 실패 · 2 시험 불가.
"""

import argparse
import json
import secrets
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path

from .. import namespace
from . import checks
from .fixtures import Fixtures
from .kube import Kubectl, ProbeError
from .model import Kind, Result, Status, exit_code
from .profile import Target, read_env, targets


def _stop(signum, _frame):
    # SIGTERM에도 finally의 정리가 돌도록 예외로 바꾼다
    raise SystemExit(128 + signum)


def create_namespace(kube: Kubectl, name: str, profile: dict[str, str]) -> None:
    # 서버 측 apply: 자동 생성된 default 서비스 계정도 경고 없이 덮어쓴다. 다시 돌려도 같은 상태가 된다
    out = kube.run(
        "apply", "--server-side", "--field-manager=rehearsal-runner", "-f", "-",
        stdin=namespace.render(name, profile),
    )
    if out.returncode != 0:
        raise ProbeError(f"리허설 네임스페이스 적용 실패: {out.stderr.strip()}")


def run(kube: Kubectl, fx: Fixtures, allowed: list[Target]) -> list[Result]:
    try:
        fx.create(kube)
    except ProbeError as e:
        return [Result("P0", "시험용 리소스를 만든다", Kind.CONTROL, Status.FAIL, str(e))]
    ready = checks.pods_ready(kube, fx)
    if ready.status is not Status.PASS:
        return [ready]
    control = checks.controls(kube, fx, allowed)
    if any(r.status is not Status.PASS for r in control):
        # 대조가 깨지면 막힘 결과를 믿을 수 없으므로 격리 시험을 돌리지 않는다
        return [ready, *control]
    try:
        return [ready, *control, *checks.isolation(kube, fx, allowed)]
    except ProbeError as e:
        return [ready, *control, Result("N0", "격리 시험 준비", Kind.CONTROL, Status.FAIL, str(e))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rehearsal_runner.isolation")
    parser.add_argument("--namespace", required=True, help="시험할 리허설 네임스페이스")
    parser.add_argument("--create", action="store_true", help="템플릿으로 네임스페이스를 만든 뒤 시험")
    parser.add_argument("--profile", required=True, type=Path, help="목적지 프로필 (profiles/<환경>.env)")
    parser.add_argument("--context", help="kubeconfig 컨텍스트 (없으면 현재 컨텍스트)")
    parser.add_argument("--image", default="busybox:1.36", help="시험용 Pod 이미지 (nc · httpd · nslookup 필요)")
    parser.add_argument("--report", type=Path, help="JSON 보고서 경로")
    args = parser.parse_args(argv)

    signal.signal(signal.SIGTERM, _stop)
    profile = read_env(args.profile)
    kube = Kubectl(args.context)
    fx = Fixtures(args.namespace, secrets.token_hex(3), args.image)
    started = datetime.now(UTC)
    try:
        if args.create:
            create_namespace(kube, args.namespace, profile)
        results = run(kube, fx, targets(profile))
    except ProbeError as e:
        results = [Result("P0", "리허설 네임스페이스를 만든다", Kind.CONTROL, Status.FAIL, str(e))]
    finally:
        fx.cleanup(kube)
    code = exit_code(results)

    for r in results:
        print(f"{r.status.upper():<12} {r.kind:<9} {r.id:<4} {r.title}  {r.detail}")
    print(f"종료 코드 {code} (0 통과 · 1 격리 실패 · 2 시험 불가)")

    report = {
        "namespace": args.namespace,
        "context": args.context,
        "run_id": fx.run_id,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "exit_code": code,
        "results": [r.to_dict() for r in results],
    }
    path = args.report or Path(f"isolation-report-{args.namespace}-{fx.run_id}.json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"보고서: {path}")
    return code


if __name__ == "__main__":
    sys.exit(main())
