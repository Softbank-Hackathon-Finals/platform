"""부하 전 사전 점검. 실패하면 부하를 한 건도 보내지 않는다.

비밀은 되풀이하지 않는다. 오류는 종류만 적고, 응답 본문과 주소는 적지 않는다.
"""

import statistics
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
import psycopg

from bravo_loadgen.load import ENV_HEADER

HEALTH_PATH = "/api/health/db"
HEALTH_SAMPLES = 5
HEALTH_TIMEOUT_S = 5
# demo-chat은 Pod마다 자기 쓰기 차단 상태를 여기에 적는다.
INFO_PATH = "/api/info"
TARGET_WRITE_SWITCH = "infra/envs/target-workloads/write_switch.py"
DB_CONNECT_TIMEOUT_S = 5
EDGE_HEADER = "CF-RAY"  # Cloudflare를 지난 응답에 붙는다
# 창 문서 검사에서 거부되는 빈 문서. 수신부는 토큰 → 계획 연결 → 문서 순서로 보므로 저장 없이 앞의 둘이 확인된다.
WINDOW_PROBE_BODY = b"{}"
WINDOW_PROBE_EXPECTED = (422, "invalid_window")
# Date 헤더는 초 단위라 그만큼 여유를 둔다.
DATE_RESOLUTION_S = 1


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _rtt_summary(rtts: list[float]) -> dict | None:
    if not rtts:
        return None
    return {
        "samples": len(rtts),
        "p50": round(statistics.median(rtts), 3),
        "max": round(max(rtts), 3),
    }


def _is_app_answer(response: httpx.Response) -> bool:
    try:
        body = response.json()
    except ValueError:
        return False
    return isinstance(body, dict) and body.get("ok") is True


def _probe_target(client: httpx.Client) -> tuple[list[httpx.Response], list[float], Check]:
    """/readyz는 앱이 떠 있다는 뜻일 뿐이다. DB까지 닿는 답을 정해진 횟수만큼 받아야 통과다."""
    responses, rtts = [], []
    for _ in range(HEALTH_SAMPLES):
        started = time.perf_counter()
        try:
            response = client.get(HEALTH_PATH, timeout=HEALTH_TIMEOUT_S)
        except httpx.HTTPError as error:
            detail = f"{HEALTH_PATH} 접속 실패: {type(error).__name__}"
            return responses, rtts, Check("대상 상태", False, detail)
        rtts.append((time.perf_counter() - started) * 1000)
        responses.append(response)
        if response.status_code != 200:
            detail = f"{HEALTH_PATH} 응답 {response.status_code}, 200이어야 한다"
            return responses, rtts, Check("대상 상태", False, detail)
        if not _is_app_answer(response):
            detail = f"{HEALTH_PATH} 200이지만 앱의 답이 아니다"
            return responses, rtts, Check("대상 상태", False, detail)
    return responses, rtts, Check("대상 상태", True, f"{HEALTH_PATH} 200 {len(responses)}회")


def _env_values(responses: list[httpx.Response]) -> Counter:
    # 길이만 줄인다. 값은 진단에 필요하고 비밀이 아니다.
    return Counter((r.headers.get(ENV_HEADER) or "(없음)")[:64] for r in responses)


def _check_env_header(values: Counter, env_pools: dict[str, str]) -> Check:
    seen = ", ".join(f"{value} {count}" for value, count in sorted(values.items()))
    if not values:
        return Check("환경 헤더", False, f"{ENV_HEADER}를 볼 응답이 없다")
    if set(values) - set(env_pools):
        return Check("환경 헤더", False, f"{ENV_HEADER}가 등록된 값이 아니다: {seen}")
    return Check("환경 헤더", True, f"{ENV_HEADER}: {seen}")


def _check_edge(responses: list[httpx.Response]) -> Check:
    if responses and all(EDGE_HEADER in r.headers for r in responses):
        return Check("Edge 경유", True, f"응답마다 {EDGE_HEADER} 있음")
    return Check("Edge 경유", False, f"{EDGE_HEADER}가 없다. Edge를 지난 응답이 아니다")


def _write_blocked(response: httpx.Response) -> bool | None:
    """읽을 수 없으면 None이다. 열렸다고 짐작하지 않는다."""
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    value = body.get("write_blocked") if isinstance(body, dict) else None
    return value if isinstance(value, bool) else None


def _describe_write_state(env: str, states: list[bool | None]) -> str:
    if not states:
        return f"{env} 응답 없어 확인 못 함"
    counts = Counter(states)
    labels = ((False, "열림"), (True, "닫힘"), (None, "확인 못 함"))
    return f"{env} " + ", ".join(f"{label} {counts[state]}" for state, label in labels if counts[state])


def _check_write_block(client: httpx.Client, env_pools: dict[str, str], write_rps: float) -> Check:
    """쓰기 차단은 Pod마다 따로이고 앱이 다시 뜨면 켜진다. 켜진 환경으로 간 쓰기는 전부 503이다."""
    name = "쓰기 차단"
    if write_rps == 0:
        return Check(name, True, "쓰기 부하가 없어 보지 않았다")
    states: dict[str, list[bool | None]] = {env: [] for env in env_pools}
    for _ in range(HEALTH_SAMPLES):
        try:
            response = client.get(INFO_PATH, timeout=HEALTH_TIMEOUT_S)
        except httpx.HTTPError as error:
            return Check(name, True, f"{INFO_PATH} 접속 실패: {type(error).__name__}. 확인 못 함")
        env = response.headers.get(ENV_HEADER)
        if env in states:  # 등록되지 않은 값은 환경 헤더 항목이 잡는다
            states[env].append(_write_blocked(response))
    detail = " · ".join(_describe_write_state(env, found) for env, found in states.items())
    closed = {env_pools[env] for env, found in states.items() if True in found}
    target_seen = any(found for env, found in states.items() if env_pools[env] == "target")
    if "target" in closed:
        return Check(name, False, f"{detail}. 목적지는 {TARGET_WRITE_SWITCH} open으로 연다")
    if closed:
        return Check(name, False, f"{detail}. 출발지 담당에게 쓰기 차단을 풀어 달라고 한다")
    if not target_seen:
        # 가중치 0이면 Edge가 목적지로 보내지 않는다. 목적지 Pod 전부는 스위치 도구가 본다.
        return Check(name, True, f"{detail}. 목적지는 {TARGET_WRITE_SWITCH} status로 본다")
    return Check(name, True, detail)


def _check_clock(response: httpx.Response | None, max_skew_s: int) -> Check:
    """시계가 어긋나면 창이 CLOCK_SKEW로 전부 UNKNOWN이 되고 반영 시간도 틀어진다."""
    value = response.headers.get("Date") if response is not None else None
    if value is None:
        return Check("시계", True, "응답에 Date가 없어 확인하지 못했다")
    try:
        skew = abs((datetime.now(UTC) - parsedate_to_datetime(value)).total_seconds())
    except (TypeError, ValueError):
        return Check("시계", True, "응답의 Date를 읽지 못해 확인하지 못했다")
    if skew > max_skew_s + DATE_RESOLUTION_S:
        return Check("시계", False, f"시계가 {skew:.0f}초 어긋났다. 허용 {max_skew_s}초")
    return Check("시계", True, f"차이 {skew:.0f}초 이내")


def _probe_window_receiver(
    client: httpx.Client, url: str, token: str, timeout_s: float
) -> tuple[httpx.Response | None, Check]:
    """진짜 창을 보내면 저장되고 순서 규칙에 걸린다. 검사에서 거부되는 빈 문서로 앞 단계만 확인한다."""
    name = "창 수신"
    try:
        response = client.post(
            url,
            content=WINDOW_PROBE_BODY,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=timeout_s,
        )
    except httpx.HTTPError as error:
        return None, Check(name, False, f"접속 실패: {type(error).__name__}")
    status = response.status_code
    if status == 401:
        return response, Check(name, False, "토큰을 거부했다")
    if status == 403:
        return response, Check(name, False, "이 토큰의 계획 연결이 꺼졌거나 만료됐다")
    if 200 <= status < 300:
        return response, Check(name, False, "빈 문서를 받아 줬다. 창 수신 경로가 아니다")
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    if (status, detail) != WINDOW_PROBE_EXPECTED:
        return response, Check(name, False, f"응답 {status}, 창 수신의 거부 응답이 아니다")
    return response, Check(name, True, "주소 · 토큰 · 계획 연결 확인, 저장하지 않음")


def check_database(name: str, dsn: str, sql: str) -> Check:
    try:
        with psycopg.connect(dsn, connect_timeout=DB_CONNECT_TIMEOUT_S, autocommit=True) as connection:
            connection.execute(sql)
    except psycopg.Error as error:
        # 메시지에 접속 정보가 섞일 수 있어 종류만 남긴다.
        return Check(name, False, f"접속 실패: {type(error).__name__}")
    return Check(name, True, "연결됨")


def run_checks(
    *,
    base_url: str,
    env_pools: dict[str, str],
    measurement_path: str,
    max_clock_skew_s: int,
    write_rps: float,
    window_url: str | None = None,
    window_token: str | None = None,
    window_timeout_s: float = 2,
    db_dsn: str | None = None,
    lock_dsn: str | None = None,
) -> dict:
    """모든 항목을 끝까지 본다. 한 번에 고칠 수 있게 실패를 전부 보여 준다."""
    checks: list[Check] = []
    notes: list[str] = []
    # trust_env=False · 리다이렉트 안 따라감: 부하와 같은 경로를 본다.
    with httpx.Client(base_url=base_url, trust_env=False) as client:
        responses, rtts, health = _probe_target(client)
        checks.append(health)
        values = _env_values(responses)
        checks.append(_check_env_header(values, env_pools))
        if measurement_path == "edge":
            checks.append(_check_edge(responses))
        checks.append(_check_write_block(client, env_pools, write_rps))
        clock_source = responses[-1] if responses else None
        if window_url:
            answer, check = _probe_window_receiver(client, window_url, window_token, window_timeout_s)
            checks.append(check)
            clock_source = answer  # 판정은 플랫폼 시계로 한다
        else:
            notes.append("창을 보내지 않는다. windows.jsonl만 남는다")
    checks.append(_check_clock(clock_source, max_clock_skew_s))
    if db_dsn:
        checks.append(check_database("대조 DB", db_dsn, "SELECT 1 FROM messages LIMIT 1"))
    else:
        notes.append("대조 DB가 없다. 끝나면 NOT_RECONCILED(종료 코드 2)이고 나중에 reconcile로 대조한다")
    if lock_dsn:
        checks.append(check_database("잠금 DB", lock_dsn, "SELECT 1"))
    return {
        "passed": all(check.ok for check in checks),
        "checked_at": datetime.now(UTC).isoformat(),
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks],
        "notes": notes,
        # 회선 + 앱 + DB의 왕복이다. 부하 중 p95를 읽을 때 회선 몫을 가늠하는 기준값이다.
        "health_rtt_ms": _rtt_summary(rtts),
        "env_values": dict(values),
    }


def render_lines(summary: dict) -> list[str]:
    lines = [f"  {'ok  ' if c['ok'] else 'FAIL'} {c['name']}: {c['detail']}" for c in summary["checks"]]
    lines += [f"  참고 {note}" for note in summary["notes"]]
    rtt = summary["health_rtt_ms"]
    if rtt:
        lines.append(f"  참고 왕복 p50 {rtt['p50']}ms, 최대 {rtt['max']}ms ({rtt['samples']}회, 회선 + 앱 + DB)")
    return lines
