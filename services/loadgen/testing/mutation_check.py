#!/usr/bin/env python3
"""시험이 정말로 재는지 본다. 코드를 일부러 망가뜨려 시험이 실패하는지 확인한다.

    uv run python testing/mutation_check.py [--out 결과.json]

사본에서만 고친다. 잠금을 놓는 코드처럼 실제 DB가 있어야 잡히는 변이는 TEST_DATABASE_URL이 있어야 돈다.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]

# (이름, 파일, 원본, 변이, 돌릴 시험, 실제 DB 필요)
MUTATIONS = [
    ("유실을 세지 않음", "reconcile.py", "if ack is not None and not rows:", "if False:", "test_reconcile.py", False),
    ("행 2개는 중복이 아님", "reconcile.py", "if len(rows) > 1:", "if len(rows) > 2:", "test_reconcile.py", False),
    ("다른 키의 복사본을 찾지 않음", "reconcile.py", "elif marker and marker.group(1) in logical_ids:", "elif False:", "test_reconcile.py", False),
    ("유령 행을 세지 않음", "reconcile.py", "phantoms.append(row)", "pass", "test_reconcile.py", False),
    ("응답 없이 저장된 것을 확인됨으로 셈", "reconcile.py", 'return "failed_but_stored" if rows else "failed_not_stored"', 'return "confirmed" if rows else "failed_not_stored"', "test_reconcile.py", False),
    ("내용 불일치를 보지 않음", "reconcile.py", "if not same_content or (ack is not None and ack[\"message_id\"] != row[\"id\"]):", "if False:", "test_reconcile.py", False),
    ("판정 불가를 통과로 씀", "reconcile.py", 'verdict = "FAIL" if reasons else "UNKNOWN" if unknowns else "PASS"', 'verdict = "FAIL" if reasons else "PASS"', "test_reconcile.py", False),
    ("기록이 불완전해도 통과", "reconcile.py", 'if not _journal_complete(end, attempts):', "if False:", "test_reconcile.py", False),
    ("503도 쓰기 응답으로 셈", "reconcile.py", '        and r["acked"]\n', "", "test_reconcile.py", False),
    ("끝까지 멈춘 공백을 놓침", "reconcile.py", "if acks[-1] < load_end_s:", "if False:", "test_reconcile.py", False),
    ("창 길이가 1초 어긋남", "windows.py", "end = start + timedelta(seconds=self.window_seconds)", "end = start + timedelta(seconds=self.window_seconds + 1)", "test_windows.py", False),
    ("끊긴 창을 보지 않음", "windows.py", "elif start > previous_end:", "elif False:", "test_windows.py", False),
    ("날짜 형식 검사를 끔", "schema.py", "format_checker=FormatChecker(),", "", "test_windows.py", False),
    ("창이 아닌 문서도 통과", "schema.py", '{"$ref": "#/$defs/SliWindow", "$defs": schema["$defs"]}', "schema", "test_windows.py", False),
    ("시간 초과 · 연결 실패를 응답처럼 넘김", "load.py", 'failure = None if outcome == "http" else outcome', "failure = None", "test_load.py", False),
    ("환경 헤더를 창에 넘기지 않음", "load.py", "Record(env, status, latency_ms, failure)", "Record(None, status, latency_ms, failure)", "test_load.py", False),
    ("지연을 창에 넘기지 않음", "load.py", "Record(env, status, latency_ms, failure)", "Record(env, status, None, failure)", "test_load.py", False),
    ("창 경계를 응답 시각이 아니라 고정 창으로 셈", "windows.py", "index = int(at // self.window_seconds)", "index = 0", "test_windows.py", False),
    ("등록한 환경을 넘기지 않음", "windows.py", "pools=self.env_pools,", "pools={},", "test_windows.py", False),
    ("리포트 백분위가 창과 다름", "stats.py", "math.ceil(fraction * len(values)) - 1", "math.ceil(fraction * len(values)) // 2", "test_stats.py", False),
    ("응답을 기다려 다음 요청을 보냄", "load.py", "group.create_task(send(due))", "await send(due)", "test_load.py", False),
    ("Retry-After를 무시함", "load.py", "delay = retry_delay(retry_after, attempt_no - 1)", "delay = 0.01", "test_load.py", False),
    ("409도 재시도함", "load.py", 'final = record["acked"] or (status is not None and 400 <= status < 500 and status != 429)', 'final = record["acked"]', "test_load.py", False),
    ("2xx면 저장 확인 없이 성공", "load.py", "acked = status in (200, 201) and message_id is not None", "acked = status in (200, 201)", "test_load.py", False),
    ("재시도 한도를 무시함", "load.py", "final = attempt_no >= config.max_attempts or delay >= deadline - self._now()", "final = False", "test_load.py", False),
    ("마지막 창을 기다리지 않음", "load.py", "await asyncio.sleep(config.duration_s - ended_at)", "pass", "test_load.py", False),
    ("끝나지 않은 창도 냄", "load.py", "                if stop.is_set():\n                    return\n", "", "test_load.py", False),
    ("DB 없이도 통과", "report.py", 'verdict = reconciliation["verdict"] if reconciliation else "UNKNOWN"', 'verdict = reconciliation["verdict"] if reconciliation else "PASS"', "test_report.py", False),
    ("로컬 측정을 Edge 측정으로 표시", "report.py", 'edge_measured = measurement_path == "edge"', "edge_measured = True", "test_report.py", False),
    ("헤더 없는 응답을 계약 오류에서 뺌", "report.py", 'return _is_http_error(attempt) or attempt["env"] not in env_pools', "return _is_http_error(attempt)", "test_report.py", False),
    ("창 집계 누락을 보지 않음", "report.py", '"conserved": counted + tail_requests + late_requests == len(attempts),', '"conserved": True,', "test_report.py", False),
    ("DB 오류에 연결 문자열을 노출", "cli.py", 'print(f"DB를 읽지 못했다: {type(error).__name__}", file=sys.stderr)', 'print(f"DB를 읽지 못했다: {db_dsn}", file=sys.stderr)', "test_cli.py", False),
    ("표본 잠금을 놓지 않음", "lock_probe.py", "self._dsn, autocommit=True,", "self._dsn, autocommit=False,", "test_lock_probe.py", True),
    ("대기를 잠금 전에만 잼", "lock_probe.py", "SELECT clock_timestamp(), pg_advisory_xact_lock(%(key)s::bigint), clock_timestamp()", "SELECT clock_timestamp(), clock_timestamp(), clock_timestamp()", "test_lock_probe.py", True),
    ("끝까지 막힌 표본을 버림", "lock_probe.py", "self.samples.append(LockSample(phase, elapsed_ms, 0, True, elapsed_ms))", "pass", "test_lock_probe.py", True),
    ("막힌 동안의 요청을 세지 않음", "lock_probe.py", "delayed = int(sample.wait_ms // interval_ms)", "delayed = 0", "test_lock_probe.py", False),
    ("멈춘 DB를 세지 않음", "lock_probe.py", '"stalled": sum(s.elapsed_ms > STALL_MS for s in samples),', '"stalled": 0,', "test_lock_probe.py", False),
    ("잘못된 DSN 오류로 측정기가 죽음", "lock_probe.py", "except psycopg.Error:", "except psycopg.OperationalError:", "test_lock_probe.py", False),
    ("깨진 응답에 실행이 무너짐", "load.py", "except httpx.RequestError:", "except httpx.TransportError:", "test_load.py", False),
    ("429를 재시도하지 않음", "load.py", "status != 429)", "True)", "test_load.py", False),
    ("남은 한도로 시간 초과를 줄이지 않음", "load.py", "min(config.attempt_timeout_s, max(deadline - self._now(), 0.001))", "config.attempt_timeout_s", "test_load.py", False),
    ("다른 키의 복사본만 있어도 저장으로 봄", "reconcile.py", 'str(row["idempotency_key"]) == logical\n            and ', "", "test_reconcile.py", False),
    ("부하 뒤 응답을 공백에 넣음", "reconcile.py", '        and r["finished_at_s"] <= load_end_s\n', "", "test_reconcile.py", False),
    ("창 0개여도 판정에 쓸 수 있음", "report.py", 'warnings.append("NO_WINDOWS")', "pass", "test_report.py", False),
    ("창이 모자라도 판정에 쓸 수 있음", "report.py", 'warnings.append("WINDOW_COUNT_SHORT")', "pass", "test_report.py", False),
    ("밀린 속도를 명목 시간으로 나눔", "report.py", "return round(len(sent) / max([duration, *sent]), 3)", "return round(len(sent) / duration, 3)", "test_report.py", False),
    ("생성기 정지를 p95로만 봄", "report.py", ' or lag["max"] > GENERATOR_LAG_MAX_WARN_MS', "", "test_report.py", False),
    ("이전 결과 폴더를 덮어씀", "cli.py", "if any((args.out / name).exists() for name in RESULT_FILES):", "if False:", "test_cli.py", False),
    ("스킴 없는 주소를 받아들임", "cli.py", 'if url.scheme not in ("http", "https") or not url.host:', "if False:", "test_cli.py", False),
    ("창 전송에 토큰을 붙이지 않음", "cli.py", 'headers={"Authorization": f"Bearer {window_token}"},', "", "test_cli.py", False),
    ("토큰 없이도 시작함", "cli.py", "window_token = _window_token() if args.window_url else None", "window_token = os.environ.get(WINDOW_TOKEN_ENV)", "test_cli.py", False),
    ("토큰 형식을 보지 않음", "cli.py", "if not BEARER_TOKEN.fullmatch(value):", "if False:", "test_cli.py", False),
    ("토큰 뒤의 줄바꿈을 통과시킴", "cli.py", "if not BEARER_TOKEN.fullmatch(value):", "if not BEARER_TOKEN.match(value):", "test_cli.py", False),
    ("형식 오류에 토큰을 되풀이함", "cli.py", 'f"환경변수 {WINDOW_TOKEN_ENV}의 토큰 형식이 잘못됐다"', 'f"{WINDOW_TOKEN_ENV} 값 {value}의 형식이 잘못됐다"', "test_cli.py", False),
    ("토큰을 부하 대상에도 보냄", "cli.py", "httpx.AsyncClient(base_url=args.base_url, timeout=None, limits=limits, trust_env=False)", 'httpx.AsyncClient(base_url=args.base_url, timeout=None, limits=limits, trust_env=False, headers={"Authorization": f"Bearer {window_token}"})', "test_cli.py", False),
    ("리다이렉트를 따라가 토큰을 넘김", "cli.py", "httpx.AsyncClient(timeout=WINDOW_POST_TIMEOUT_S, trust_env=False)", "httpx.AsyncClient(timeout=WINDOW_POST_TIMEOUT_S, trust_env=False, follow_redirects=True)", "test_cli.py", False),
    ("리포트와 실행 상태 파일에 토큰을 남김", "cli.py", '"target": args.base_url,', '"target": f"{args.base_url}#{window_token}",', "test_cli.py::test_token_is_left_in_no_file_output_or_log", False),
    ("창 파일에 토큰을 남김", "cli.py", "windows_file.write(json.dumps(window, ensure_ascii=False)", 'windows_file.write(json.dumps({**window, "token": window_token}, ensure_ascii=False)', "test_cli.py::test_token_is_left_in_no_file_output_or_log", False),
    ("표준 오류에 토큰을 남김", "cli.py", "windows_file.flush()", "windows_file.flush(); print(window_token, file=sys.stderr)", "test_cli.py::test_token_is_left_in_no_file_output_or_log", False),
    ("로그에 토큰을 남김", "cli.py", "windows_file.flush()", 'windows_file.flush(); __import__("logging").getLogger("bravo").warning(window_token)', "test_cli.py::test_token_is_left_in_no_file_output_or_log", False),
    ("창 주소의 자격 증명을 받아들임", "cli.py", "if url.userinfo:", 'if url.userinfo and option == "--base-url":', "test_cli.py", False),
    ("창 주소를 검사하지 않음", "cli.py", 'if args.window_url and (problem := _url_problem("--window-url", args.window_url)):', "if False:", "test_cli.py", False),
    ("예상 못 한 오류를 그대로 던짐", "cli.py", "    except Exception as error:", "    except ZeroDivisionError as error:", "test_cli.py", False),
    ("멈춘 DB를 끝없이 기다림", "cli.py", "reader.join(DB_READ_TIMEOUT_S)", "reader.join()", "test_cli.py", False),
    ("부하 끝난 뒤에도 부하 표본으로 셈", "cli.py", 'config.duration_s, lambda: setattr(probe, "phase", "drain")', 'config.duration_s, lambda: None', "test_cli.py", False),
    ("연결 문자열을 되풀이함", "cli.py", 'raise LookupError("DSN 옵션에는 환경변수 이름을 넣는다")', 'raise LookupError(f"{variable}가 이상하다")', "test_cli.py", False),
    ("읽기 전용을 풀어 둠", "db.py", "connection.read_only = True", "pass", "test_db.py", True),
]


def run_mutation(workdir: Path, name, filename, old, new, test_file) -> dict:
    target = workdir / "services/loadgen/src/bravo_loadgen" / filename
    original = target.read_text()
    if original.count(old) != 1:
        return {"mutation": name, "file": filename, "caught": False, "error": "원본 문자열이 정확히 1번 나오지 않는다"}
    target.write_text(original.replace(old, new))
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider", f"tests/{test_file}"],
            cwd=workdir / "services/loadgen",
            env={**os.environ, "PYTHONPATH": str(workdir / "services/loadgen/src")},
            capture_output=True,
            text=True,
            timeout=120,
        )
        caught = result.returncode == 1
    except subprocess.TimeoutExpired:
        # 변이가 시험을 끝나지 못하게 만든 경우. 실패로 잡힌 것으로 본다.
        caught = True
    finally:
        target.write_text(original)
    return {"mutation": name, "file": filename, "test": test_file, "caught": caught}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    has_db = bool(os.environ.get("TEST_DATABASE_URL"))
    results = []
    with tempfile.TemporaryDirectory() as temp:
        workdir = Path(temp)
        ignore = shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache", ".ruff_cache")
        shutil.copytree(ROOT, workdir / "services/loadgen", ignore=ignore)
        # bravo_observability는 설치된 것을 쓴다. 스키마만 저장소 상대 경로로 읽는다.
        shutil.copytree(REPO / "docs/observability", workdir / "docs/observability")
        for name, filename, old, new, test_file, needs_db in MUTATIONS:
            if needs_db and not has_db:
                results.append({"mutation": name, "file": filename, "caught": None, "skipped": "TEST_DATABASE_URL 없음"})
                continue
            results.append(run_mutation(workdir, name, filename, old, new, test_file))
            print(f"{'잡힘' if results[-1]['caught'] else '놓침'}  {name}", flush=True)
    missed = [r for r in results if r["caught"] is False]
    summary = {"total": len(results), "caught": sum(r["caught"] is True for r in results), "missed": len(missed), "results": results}
    if args.out:
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    print(f"변이 {summary['total']}건, 잡힘 {summary['caught']}건, 놓침 {summary['missed']}건")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())
