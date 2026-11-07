"""명령줄 진입점. 종료 코드: 0 통과 · 1 실패 · 2 시험 불가."""

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import resource
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
from psycopg.conninfo import conninfo_to_dict

from bravo_loadgen.align import AlignmentError, align_run, render_alignment
from bravo_loadgen.db import fetch_rows
from bravo_loadgen.journal import Journal, read_journal, run_author
from bravo_loadgen.load import LoadConfig, LoadRunner
from bravo_loadgen.lock_probe import LockProbe, summarize_lock_samples
from bravo_loadgen.preflight import render_lines, run_checks
from bravo_loadgen.report import build_report, exit_code, render_markdown
from bravo_loadgen.schema import SchemaViolation, load_profile, validate_window
from bravo_loadgen.signin import CLIENT_SECRET_ENV, PASSWORD_ENV, TokenSupply, fetcher
from bravo_loadgen.windows import check_continuity

DEFAULT_WINDOW_SECONDS = 10
WINDOW_POST_TIMEOUT_S = 2
DB_READ_TIMEOUT_S = 120
RESULT_FILES = ("journal.jsonl", "windows.jsonl", "run-state.json", "report.json", "report.md")
WINDOW_TOKEN_ENV = "BRAVO_WINDOW_TOKEN"
# RFC 6750 b64token. 헤더에 못 넣는 값은 부하 중간에 값이 담긴 오류로 터지므로 시작 전에 막는다.
BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*")

MEASUREMENT_PATHS = ("edge", "local-mock-edge", "local-direct")
# platform-job (T49): the platform's own Kubernetes Job; its windows reach T10 through the Job log (--window-stdout).
GENERATORS = ("loadgen-ec2", "laptop", "platform-job")
# infra/envs/loadgen-aws의 cloud-init이 첫 부팅 끝에 만든다. 다른 컴퓨터에는 없다.
EC2_MARKER = Path("/var/lib/bravo-loadgen-ready")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return args.handler(args)
    except Exception as error:
        # 1은 "실패 있음"이다. 예상 못 한 오류는 시험 불가다. 메시지에 접속 정보가 섞일 수 있어 종류만 남긴다.
        kinds = [type(e).__name__ for e in getattr(error, "exceptions", [error])]
        return _fail(f"예상 못 한 오류 {', '.join(kinds)}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bravo-loadgen", description=__doc__)
    commands = parser.add_subparsers(required=True)

    run = commands.add_parser("run", help="부하를 넣고 창 요약과 대조 리포트를 만든다")
    _add_load_arguments(run)
    run.set_defaults(handler=_cmd_run)

    check_only = commands.add_parser("preflight", help="부하 없이 사전 점검만 한다. 인자는 measure와 같다")
    _add_load_arguments(check_only)
    check_only.set_defaults(handler=_cmd_preflight)

    measure = commands.add_parser("measure", help="사전 점검을 통과해야 부하를 넣는다")
    _add_load_arguments(measure)
    measure.set_defaults(handler=_cmd_measure)

    again = commands.add_parser("reconcile", help="끝난 실행의 기록을 지금 DB와 다시 대조한다")
    again.add_argument("--out", type=Path, required=True, help="run이 만든 산출물 폴더")
    again.add_argument("--db-dsn-env", required=True)
    again.set_defaults(handler=_cmd_reconcile)

    check = commands.add_parser("check-windows", help="창 요약 파일의 계약 · 연속성을 검사한다")
    check.add_argument("path", type=Path)
    check.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_SECONDS)
    check.set_defaults(handler=_cmd_check_windows)

    aligned = commands.add_parser("align", help="가중치 영수증과 창을 같은 실행 ID로 맞춘다")
    aligned.add_argument("--out", type=Path, required=True, help="run이 만든 산출물 폴더")
    aligned.add_argument("--receipts", type=Path, required=True, help="가중치 영수증 JSONL")
    aligned.set_defaults(handler=_cmd_align)
    return parser


def _add_load_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-url", required=True, help="요청을 보낼 주소. 실제 판정용은 Edge 주소")
    parser.add_argument("--plan-id", required=True)
    parser.add_argument("--source-env", required=True, help="출발지의 APP_ENV 값 (계획에 등록한 값)")
    parser.add_argument("--target-env", required=True, help="목적지의 APP_ENV 값 (계획에 등록한 값)")
    parser.add_argument("--duration", type=float, required=True, help="부하 시간(초)")
    parser.add_argument("--measurement-path", required=True, choices=MEASUREMENT_PATHS)
    parser.add_argument("--generator", choices=GENERATORS, help="부하를 내는 곳. edge 경로에는 필수")
    parser.add_argument("--out", type=Path, required=True, help="산출물 폴더")
    parser.add_argument("--run-id", help="없으면 시각으로 만든다. preflight · measure에는 필수")
    parser.add_argument("--write-rps", type=float, default=50)
    parser.add_argument("--read-rps", type=float, default=150)
    parser.add_argument("--window-seconds", type=int, default=DEFAULT_WINDOW_SECONDS)
    parser.add_argument("--attempt-timeout", type=float, default=15)
    parser.add_argument("--retry-budget", type=float, default=60)
    parser.add_argument("--window-url", help=f"창 요약을 POST할 플랫폼 주소. 토큰은 환경변수 {WINDOW_TOKEN_ENV}")
    parser.add_argument("--window-stdout", action="store_true",
                        help="닫힌 창을 표준 출력에도 'BRAVO_WINDOW <json>' 한 줄로 낸다(플랫폼 Job: T10이 로그에서 받는다)")
    parser.add_argument("--db-dsn-env", help="대조용 DB 연결 문자열이 든 환경변수 이름")
    parser.add_argument("--lock-dsn-env", help="잠금 측정용 DB 연결 문자열이 든 환경변수 이름")
    parser.add_argument("--lock-baseline", type=float, default=3, help="부하 전 잠금 기준 측정(초)")
    parser.add_argument("--auth-issuer",
                        help="로그인을 켠 앱의 AUTH_ISSUER. 주면 쓰기에 ID 토큰을 싣고 작성자를 토큰 이름으로 쓴다(T47). "
                             f"비밀번호는 환경변수 {PASSWORD_ENV}, Cognito가 아니면 클라이언트 비밀도 {CLIENT_SECRET_ENV}")
    parser.add_argument("--auth-client-id", help="부하 생성기 전용 클라이언트 ID. 브라우저 클라이언트는 비밀번호를 받지 않는다")
    parser.add_argument("--auth-username", help="부하 생성기 계정 이름. 메시지 작성자로 보인다")
    parser.add_argument("--fresh-connections", action="store_true",
                        help="요청마다 새 연결을 연다(서로 다른 사용자처럼). 실제 Cloudflare LB는 연결마다 pool을 고르는 것으로 "
                             "관측돼, keep-alive 연결 몇 개로는 가중치 분배를 잴 수 없다(T17 2026-11-08)")


def _fail(message: str) -> int:
    print(f"시험 불가: {message}", file=sys.stderr)
    return 2


def _dsn(variable: str | None) -> str | None:
    """연결 문자열은 비밀이라 명령줄 인자로 받지 않는다."""
    if variable is None:
        return None
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
        # 연결 문자열을 그대로 넣는 실수일 수 있다. 받은 값을 되풀이하지 않는다.
        raise LookupError("DSN 옵션에는 환경변수 이름을 넣는다")
    value = os.environ.get(variable)
    if not value:
        raise LookupError(f"환경변수 {variable}가 비어 있다")
    try:
        conninfo_to_dict(value)
    except psycopg.Error:
        raise LookupError(f"환경변수 {variable}의 연결 문자열 형식이 잘못됐다") from None
    return value


def _window_token() -> str:
    """토큰은 비밀이라 명령줄 인자로 받지 않는다. 인자는 프로세스 목록에 보인다."""
    value = os.environ.get(WINDOW_TOKEN_ENV)
    if not value:
        raise LookupError(f"--window-url을 쓰려면 환경변수 {WINDOW_TOKEN_ENV}가 필요하다")
    if not BEARER_TOKEN.fullmatch(value):
        raise LookupError(f"환경변수 {WINDOW_TOKEN_ENV}의 토큰 형식이 잘못됐다")
    return value


def _url_problem(option: str, value: str) -> str | None:
    """받은 값은 되풀이하지 않는다. 자격 증명이 섞여 있을 수 있다."""
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        return f"{option}이 올바른 주소가 아니다"
    if url.userinfo:
        return f"{option}에 자격 증명을 넣지 않는다"
    if url.scheme not in ("http", "https") or not url.host:
        return f"{option}은 http(s)://주소 형식이어야 한다"
    return None


def open_files_needed(
    *, write_rps: float, read_rps: float, retry_budget_s: float, attempt_timeout_s: float
) -> int:
    """대상이 답하지 않을 때 동시에 열려 있을 수 있는 연결 수. 쓰기는 재시도 한도 동안 살아 있다."""
    return math.ceil(write_rps * retry_budget_s + read_rps * attempt_timeout_s) + 256


def _raise_open_file_limit(needed: int) -> None:
    """연결이 모자라 난 실패를 서버 연결 실패로 잘못 세지 않게 미리 확보한다."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft == resource.RLIM_INFINITY or soft >= needed:
        return
    if hard != resource.RLIM_INFINITY and hard < needed:
        raise OSError(f"열 수 있는 파일 수 상한({hard})이 필요한 값({needed})보다 작다")
    resource.setrlimit(resource.RLIMIT_NOFILE, (needed, hard))


@dataclass(frozen=True)
class _SignIn:
    kind: str
    supply: TokenSupply


def _sign_in(args: argparse.Namespace) -> _SignIn | None:
    """로그인은 기본으로 꺼져 있다. 세 값을 모두 줘야 켠다. 첫 토큰은 부하 전에 받는다."""
    given = [args.auth_issuer, args.auth_client_id, args.auth_username]
    if not any(given):
        return None
    if not all(given):
        raise LookupError("로그인을 켜려면 --auth-issuer, --auth-client-id, --auth-username을 모두 준다")
    kind, fetch = fetcher(args.auth_issuer, args.auth_client_id, args.auth_username)
    supply = TokenSupply(fetch)
    print(f"로그인: {kind} 발급자에서 '{supply.name}' 이름으로 토큰을 받았다")
    return _SignIn(kind, supply)


@dataclass(frozen=True)
class _Prepared:
    config: LoadConfig
    db_dsn: str | None
    lock_dsn: str | None
    window_token: str | None
    sign_in: _SignIn | None = None


def _prepare(args: argparse.Namespace) -> _Prepared:
    """요청을 보내기 전에 알 수 있는 설정 오류를 막는다. run · preflight · measure가 함께 쓴다."""
    db_dsn = _dsn(args.db_dsn_env)
    lock_dsn = _dsn(args.lock_dsn_env)
    window_token = _window_token() if args.window_url else None
    if problem := _url_problem("--base-url", args.base_url):
        raise LookupError(problem)
    if args.window_url and (problem := _url_problem("--window-url", args.window_url)):
        raise LookupError(problem)
    if any((args.out / name).exists() for name in RESULT_FILES):
        # 새 실행이 죽으면 이전 판정이 그대로 남아 잘못 읽힌다.
        raise LookupError(f"{args.out}에 이미 결과가 있다. 새 폴더를 쓴다")
    if args.source_env == args.target_env:
        raise LookupError("--source-env와 --target-env가 같다")
    if args.measurement_path == "edge" and not args.generator:
        raise LookupError("edge 경로에는 --generator로 부하를 내는 곳을 밝힌다")
    if args.generator == "loadgen-ec2" and not EC2_MARKER.exists():
        raise LookupError("--generator loadgen-ec2인데 이 컴퓨터에는 loadgen-aws 표시 파일이 없다")
    if args.measurement_path == "edge" and args.generator == "laptop" and args.window_url:
        # 창에는 실행 위치를 적을 칸이 없어, 받는 쪽은 전용 호스트의 값과 구분하지 못한다.
        raise LookupError("노트북에서 잰 창은 판정 경로로 보내지 않는다. --window-url을 뺀다")
    run_id = args.run_id or f"t18-{datetime.now(UTC):%Y%m%dt%H%M%Sz}"
    sign_in = _sign_in(args)
    config = LoadConfig(
        run_id=run_id,
        plan_id=args.plan_id,
        env_pools={args.source_env: "source", args.target_env: "target"},
        duration_s=args.duration,
        write_rps=args.write_rps,
        read_rps=args.read_rps,
        window_seconds=args.window_seconds,
        attempt_timeout_s=args.attempt_timeout,
        retry_budget_s=args.retry_budget,
        author=sign_in.supply.name if sign_in else None,
    )
    _raise_open_file_limit(
        open_files_needed(
            write_rps=config.write_rps,
            read_rps=config.read_rps,
            retry_budget_s=config.retry_budget_s,
            attempt_timeout_s=config.attempt_timeout_s,
        )
    )
    return _Prepared(config, db_dsn, lock_dsn, window_token, sign_in)


def _execute(args: argparse.Namespace, prepared: _Prepared, preflight: dict | None = None) -> int:
    args.out.mkdir(parents=True, exist_ok=True)
    state = asyncio.run(
        _run_load(prepared.config, args, prepared.lock_dsn, prepared.window_token, prepared.sign_in)
    )
    state["preflight"] = preflight
    (args.out / "run-state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
    return _write_report(args.out, prepared.db_dsn, read_journal(args.out / "journal.jsonl"))


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        prepared = _prepare(args)
    except (LookupError, ValueError, OSError) as error:
        return _fail(str(error))
    return _execute(args, prepared)


def _run_preflight(args: argparse.Namespace, prepared: _Prepared) -> dict:
    summary = run_checks(
        base_url=args.base_url,
        env_pools=prepared.config.env_pools,
        measurement_path=args.measurement_path,
        max_clock_skew_s=load_profile()["max_clock_skew_seconds"],
        write_rps=prepared.config.write_rps,
        window_url=args.window_url,
        window_token=prepared.window_token,
        window_timeout_s=WINDOW_POST_TIMEOUT_S,
        db_dsn=prepared.db_dsn,
        lock_dsn=prepared.lock_dsn,
    )
    print("\n".join(render_lines(summary)))
    print(f"사전 점검 {'통과' if summary['passed'] else '실패'}: 실행 ID {prepared.config.run_id}")
    return summary


def _prepare_with_run_id(args: argparse.Namespace) -> _Prepared:
    if not args.run_id:
        raise LookupError("--run-id가 필요하다. 가중치 변경 기록과 같은 값으로 맞춘다")
    return _prepare(args)


def _cmd_preflight(args: argparse.Namespace) -> int:
    try:
        prepared = _prepare_with_run_id(args)
    except (LookupError, ValueError, OSError) as error:
        return _fail(str(error))
    return 0 if _run_preflight(args, prepared)["passed"] else 2


def _cmd_measure(args: argparse.Namespace) -> int:
    try:
        prepared = _prepare_with_run_id(args)
    except (LookupError, ValueError, OSError) as error:
        return _fail(str(error))
    summary = _run_preflight(args, prepared)
    if not summary["passed"]:
        return _fail("사전 점검 실패. 부하를 보내지 않았다")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "preflight.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return _execute(args, prepared, summary)


async def _run_load(
    config: LoadConfig,
    args: argparse.Namespace,
    lock_dsn: str | None,
    window_token: str | None,
    sign_in: _SignIn | None = None,
) -> dict:
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=0 if args.fresh_connections else 256)
    probe = LockProbe(lock_dsn) if lock_dsn else None
    if probe:
        probe.start()
        await asyncio.sleep(args.lock_baseline)
        probe.phase = "load"
        # 부하 중 표본만 "초당 50건일 때"로 센다. 남은 응답을 기다리는 구간은 뺀다.
        asyncio.get_running_loop().call_later(
            config.duration_s, lambda: setattr(probe, "phase", "drain")
        )

    # trust_env=False: 환경변수의 프록시로 샜다가 측정 경로가 달라지지 않게 한다.
    async with (
        httpx.AsyncClient(base_url=args.base_url, timeout=None, limits=limits, trust_env=False)
    ) as client, httpx.AsyncClient(timeout=WINDOW_POST_TIMEOUT_S, trust_env=False) as poster:
        with (
            Journal(args.out / "journal.jsonl") as journal,
            (args.out / "windows.jsonl").open("w", encoding="utf-8") as windows_file,
        ):

            async def sink(window: dict) -> None:
                windows_file.write(json.dumps(window, ensure_ascii=False) + "\n")
                windows_file.flush()
                if args.window_stdout:
                    print("BRAVO_WINDOW " + json.dumps(window, ensure_ascii=False, separators=(",", ":")), flush=True)
                if args.window_url:
                    response = await poster.post(
                        args.window_url,
                        json=window,
                        headers={"Authorization": f"Bearer {window_token}"},
                    )
                    response.raise_for_status()

            refresher = asyncio.create_task(sign_in.supply.keep_fresh()) if sign_in else None
            try:
                result = await LoadRunner(
                    config, client, journal, window_sink=sink,
                    write_headers=sign_in.supply.headers if sign_in else None,
                ).run()
            finally:
                if refresher:
                    refresher.cancel()

    lock_summary = None
    if probe:
        probe.stop()
        lock_summary = summarize_lock_samples(
            probe.samples, key=probe.key, connection_errors=probe.connection_errors
        )
    return {
        "target": args.base_url,
        "measurement_path": args.measurement_path,
        "generator": args.generator,
        "connections": "fresh" if args.fresh_connections else "keepalive",
        "delivery_failed": result.delivery_failed,
        "invalid_windows": result.invalid_windows,
        "tail_requests": result.tail_requests,
        "late_requests": result.late_requests,
        "continuity_problems": result.continuity_problems,
        "lock_probe": lock_summary,
        "sign_in": sign_in.supply.summary(sign_in.kind) if sign_in else None,
    }


def _read_rows(db_dsn: str, run_id: str, logical_ids: list[str], author: str) -> list[dict] | None:
    """DB가 멈춰도 리포트 없이 영원히 기다리지 않는다. 못 읽으면 None이고 판정은 시험 불가가 된다."""
    outcome: dict[str, list[dict]] = {}

    def read() -> None:
        try:
            outcome["rows"] = fetch_rows(db_dsn, run_id, logical_ids, author)
        except psycopg.Error as error:
            # 메시지에 접속 정보가 섞일 수 있어 종류만 남긴다.
            print(f"DB를 읽지 못했다: {type(error).__name__}", file=sys.stderr)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    reader.join(DB_READ_TIMEOUT_S)
    if reader.is_alive():
        print(f"DB를 읽지 못했다: {DB_READ_TIMEOUT_S}초 안에 답이 없다", file=sys.stderr)
    return outcome.get("rows")


def _write_report(out: Path, db_dsn: str | None, records: list[dict]) -> int:
    journal_path = out / "journal.jsonl"
    run_id = records[0]["run_id"]
    rows = None
    if db_dsn:
        logical_ids = sorted(
            {r["logical_id"] for r in records if r["type"] == "attempt" and r["kind"] == "write"}
        )
        rows = _read_rows(db_dsn, run_id, logical_ids, run_author(records[0]))
    state = json.loads((out / "run-state.json").read_text())
    windows_path = out / "windows.jsonl"
    report = build_report(
        records=records,
        rows=rows,
        windows=[json.loads(line) for line in windows_path.read_text().splitlines()],
        journal_sha256=hashlib.sha256(journal_path.read_bytes()).hexdigest(),
        **state,
    )
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (out / "report.md").write_text(render_markdown(report))
    print(render_markdown(report))
    return exit_code(report)


def _cmd_reconcile(args: argparse.Namespace) -> int:
    try:
        db_dsn = _dsn(args.db_dsn_env)
    except LookupError as error:
        return _fail(str(error))
    journal_path = args.out / "journal.jsonl"
    if not (journal_path.is_file() and (args.out / "run-state.json").is_file()):
        return _fail("실행이 끝나지 않아 기록이 없다")
    records = read_journal(journal_path)
    if not records or records[0]["type"] != "run_start":
        return _fail("기록이 비어 있다")
    return _write_report(args.out, db_dsn, records)


def _cmd_check_windows(args: argparse.Namespace) -> int:
    windows = [json.loads(line) for line in args.path.read_text().splitlines()]
    if not windows:
        return _fail("창이 하나도 없다")
    bad = 0
    for window in windows:
        try:
            validate_window(window)
        except SchemaViolation as error:
            bad += 1
            print(f"스키마 위반 {window.get('window', {}).get('start')}: {error}")
    problems = check_continuity(windows, window_seconds=args.window_seconds)
    for problem in problems:
        print(f"연속성 {problem['kind']}: {problem}")
    print(f"창 {len(windows)}개, 스키마 위반 {bad}개, 연속성 문제 {len(problems)}건")
    return 1 if bad or problems else 0


def _cmd_align(args: argparse.Namespace) -> int:
    report_path, windows_path = args.out / "report.json", args.out / "windows.jsonl"
    if not (report_path.is_file() and windows_path.is_file() and args.receipts.is_file()):
        return _fail("리포트, 창 파일, 영수증 중 없는 것이 있다")
    try:
        receipts = [json.loads(line) for line in args.receipts.read_text().splitlines() if line.strip()]
        result = align_run(
            json.loads(report_path.read_text()),
            [json.loads(line) for line in windows_path.read_text().splitlines()],
            receipts,
            load_profile(),
        )
    except AlignmentError as error:
        return _fail(str(error))
    except json.JSONDecodeError:
        return _fail("영수증 파일이 JSONL 형식이 아니다")
    (args.out / "weight-alignment.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(render_alignment(result))
    return 0 if result["complete"] else 1


if __name__ == "__main__":
    sys.exit(main())
