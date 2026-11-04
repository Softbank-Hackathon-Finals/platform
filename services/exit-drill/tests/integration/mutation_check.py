"""검사를 일부러 뺐을 때 시험이 실패하는지 잰다. 시험이 통과한다는 것과 시험이 잰다는 것은 다르다.

임시 폴더의 사본만 고친다. 저장소의 소스는 건드리지 않는다.

    uv run python tests/integration/mutation_check.py --out mutation-results.json
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = "src/bravo_exit_drill"

# (파일, 찾을 글자, 바꿀 글자, 무엇을 뺐는가)
MUTATIONS = [
    ("observation.py", "if NOT_EXIT_EVIDENCE_ANNOTATION in annotations:", "if False:", "출구 제외 표지 읽기"),
    ("observation.py", "if any(name.startswith(AWS_ENDPOINT_OVERRIDE) for name in names):", "if False:", "AWS 호출을 돌리는 설정 읽기"),
    ("observation.py", "for container in [*containers, *sidecars]:", "for container in containers:", "사이드카의 설정 읽기"),
    ("observation.py", "if not isinstance(annotations, dict) or not isinstance(spec, dict):\n        return None\n", "if not isinstance(annotations, dict) or not isinstance(spec, dict):\n        return []\n", "읽지 못한 Pod를 깨끗하다고 하지 않기"),
    ("observation.py", 'if not isinstance(env, list) or not all(isinstance(e, dict) and isinstance(e.get("name"), str) for e in env):\n            return None\n', 'if not isinstance(env, list) or not all(isinstance(e, dict) and isinstance(e.get("name"), str) for e in env):\n            return []\n', "읽지 못한 환경 변수를 깨끗하다고 하지 않기"),
    ("verdict.py", '*[_check_eligibility(name, before, after) for name, w in sorted(plan["workloads"].items()) if w["role"] == "app"],\n', "", "출구 시험 자격 판정"),
    ("verdict.py", "if not was or not now:", "if False:", "Pod를 못 읽었을 때 자격 없음"),
    ("verdict.py", "found = [p.get(\"exit_blockers\") for p in [*was, *now]]", "found = [p.get(\"exit_blockers\") for p in now]", "차단 전 Pod의 자격도 보기"),
    ("verdict.py", "found = [p.get(\"exit_blockers\") for p in [*was, *now]]", "found = [p.get(\"exit_blockers\") for p in was]", "차단 뒤 Pod의 자격도 보기"),
    ("verdict.py", 'if not all(isinstance(b, list) and all(isinstance(c, str) and c for c in b) for b in found):', "if False:", "자격 값이 읽히지 않으면 UNKNOWN"),
    ("verdict.py", 'return Check(check_id, UNKNOWN, reasons[0]) if reasons else Check(check_id, PASS, "ELIGIBLE")', 'return Check(check_id, PASS, "ELIGIBLE")', "자격 없는 Pod를 통과시키지 않기"),
    ("policy.py", 'if "replication" in roles:', "if False:", "DB Pod를 고르는 선택자 거부"),
    ("policy.py", "if any(_same_path(route, keep) for keep in replication):", "if False:", "복제 경로에 건 차단 거부"),
    ("policy.py", 'if any(_also_allowed(route, allow) for allow in routes if allow.klass == "local"):', "if False:", "허용 목록에도 있는 차단 거부"),
    ("policy.py", 'covered = {role for r in routes if r.klass == "source_access" for role in r.roles}', "covered = {role for r in routes for role in r.roles}", "차단 경로로만 의존성 측정 인정"),
    ("policy.py", '    "candidate",\n', "", "계획 해시에 후보 포함"),
    ("policy.py", "elif workload.role != needed:", "elif False:", "경로 주인 역할 검사"),
    ("policy.py", 'found.append(Violation("UNDECLARED_EGRESS", f"{name}:{pod.name}"))', "pass", "기존 허용(합집합) 거부"),
    ("policy.py", 'found.append(Violation("REPLICATION_BLOCKED", f"{route.id}:{pod.name}"))', "pass", "이미 막힌 복제 경로 거부"),
    ("policy.py", "if pod.host_network:", "if False:", "hostNetwork Pod 거부"),
    ("policy.py", "if not owners:", "if False:", "명세에 없는 Pod 거부"),
    ("policy.py", 'if workload.role != "app":', 'if workload.role == "control":', "정책을 앱 역할에만 만들기"),
    ("policy.py", '"policyTypes": ["Egress"],', '"policyTypes": ["Ingress", "Egress"],', "송신만 다루기"),
    ("policy.py", 'if route.klass == "local" and _leaves_cluster(route.to):', "if False:", "클러스터 밖으로 나가는 허용 거부"),
    ("policy.py", '    "decisions",\n', "", "계획 해시에 결정 포함"),
    ("policy.py", 'found.append(Violation("SELECTOR_MATCHES_NOTHING", workload.name))', "pass", "아무것도 안 고르는 선택자 거부"),
    ("policy.py", 'if "control" in roles:', "if False:", "대조군이 차단되는 선택자 거부"),
    ("policy.py", 'if route.ports != (POSTGRES,):', "if False:", "복제 경로는 5432만"),
    ("cluster.py", "if not peers or not ports:", "if not peers:", "포트를 비운 허용 거부"),
    ("cluster.py", 'for expression in selector.get("matchExpressions") or []:', "for expression in []:", "matchExpressions 해석"),
    ("cluster.py", 'raise SnapshotError("unknown selector operator")', "pass", "모르는 선택자 연산 거부"),
    ("cluster.py", 'if metadata.get("namespace") != namespace:', "if False:", "다른 네임스페이스 읽기 거부"),
    ("cluster.py", 'if "ipBlock" in peer:\n        return False\n', 'if "ipBlock" in peer:\n        return True\n', "ipBlock으로 복제 경로를 단정하지 않기"),
    ("cluster.py", 'if "namespaceSelector" not in peer and namespace != own_namespace:\n                    continue\n', "if False:\n                    continue\n", "자기 네임스페이스만 가리키는 peer 해석"),
    ("cluster.py", 'if direction == "ingress" and not _rule_admits_port(rule, ports):', "if False:", "수신 경로의 포트 확인"),
    ("cluster.py", 'if any(e.get("key") != NAMESPACE_NAME_LABEL or e.get("operator") != "In" for e in expressions):', "if False:", "이름을 안 보는 네임스페이스 선택자 거부"),
    ("cluster.py", 'return direction == "ingress" or bool(spec.get("egress"))', 'return direction == "ingress" or "egress" in spec', "빈 egress는 송신 제한이 아님"),
    ("cluster.py", "        return True  # 이 방향을 제한하는 정책이 없으면 기본 허용이다.", "        return False", "제한 정책이 없으면 기본 허용"),
    ("cluster.py", 'if types:\n        return direction.capitalize() in types\n    return direction == "ingress" or bool(spec.get("egress"))', 'return bool(types) and direction.capitalize() in types', "policyTypes 생략 시 기본 해석"),
    ("policy.py", 'route.to.selector, route.direction, ports):', 'route.to.selector, "egress", ports):', "역방향 복제를 수신으로 보기"),
    ("verdict.py", "return Check(check_id, FAIL, fail_code)", "return Check(check_id, PASS, fail_code)", "끊긴 내부 · 복제 경로를 실패로"),
    ("verdict.py", 'return Check(check_id, FAIL, "NEW_CONNECTION_FAILED")', 'return Check(check_id, PASS, "NEW_CONNECTION_FAILED")', "실패한 새 연결을 실패로"),
    ("verdict.py", 'if any(r.get("ok") is not True for r in rows):', "if False:", "새 연결 결과가 참일 때만 통과"),
    ("verdict.py", 'if not any(r.get("state") == "streaming" for r in _stream_rows(before, route_id)):', "if False:", "차단 전 복제 동작 확인"),
    ("verdict.py", 'return Check(check_id, FAIL, "NO_PODS")', 'return Check(check_id, PASS, "NO_PODS")', "Pod가 없으면 실패"),
    ("verdict.py", "if names & ours:", "if False:", "정책이 이미 있는 차단 전 측정 거부"),
    ("verdict.py", "    if outside:\n", "    if False:\n", "계획 밖 Pod를 통과로 안 치기"),
    ("observation.py", "            outside.append(metadata.get(\"name\"))\n", "            pass\n", "계획 밖 Pod 기록"),
    ("verdict.py", "if record is None or restarts == 0:", "if False:", "컨테이너 재시작 없이는 시작 증거로 안 치기"),
    ("verdict.py", "if not all(_container_known(p, container) for p in now):", "if False:", "대상 컨테이너 상태를 모르면 판정 보류"),
    ("verdict.py", "        isinstance(container, str)\n        and ", "        ", "이름 없는 대상과 이름 없는 관측을 같다고 치지 않기"),
    ("verdict.py", '        and pod.get("container_name") == container\n', "", "계획이 정한 이름의 컨테이너인지"),
    ("verdict.py", '        and _is_id(pod.get("container_id"))\n', "", "지금 도는 컨테이너 ID가 있는지"),
    ("verdict.py", "return isinstance(value, str) and bool(value.strip())", "return isinstance(value, str)", "빈 컨테이너 ID 거부"),
    ("verdict.py", "return isinstance(value, str) and bool(value.strip())", "return isinstance(value, str) and bool(value)", "공백뿐인 컨테이너 ID 거부"),
    ("verdict.py", "return _is_id(replaced) and replaced ==", "return replaced ==", "재시작 기록의 빈 ID 거부"),
    ("verdict.py", '        and type(pod.get("restarts")) is int\n', "", "대상 컨테이너 재시작 횟수가 있는지"),
    ("verdict.py", '        and type(pod.get("other_restarts")) is int\n', "", "다른 컨테이너 재시작 횟수가 있는지"),
    ("verdict.py", 'if pod["other_restarts"] != 0:', "if False:", "차단 아래에서 다시 뜬 사이드카를 통과로 안 치기"),
    ("verdict.py", "if not _record_matches(record, pod):", "if False:", "재시작 기록과 관측 대조"),
    ("verdict.py", 'if not isinstance(record, dict) or record.get("container") != pod["container_name"]:', "if not isinstance(record, dict):", "기록의 컨테이너 이름 대조"),
    ("verdict.py", 'replaced == pod.get("previous_container_id") and ', "", "기록한 컨테이너가 실제로 끝났는지"),
    ("verdict.py", ' and replaced != pod["container_id"]', "", "지금 도는 컨테이너가 기록과 다른지"),
    ("observation.py", 'matches = [c for c in containers if c.get("name") == wanted]', "matches = containers[:1]", "첫 컨테이너를 앱으로 치지 않기"),
    ("observation.py", "if wanted is None or len(matches) != 1:", "if wanted is None or not matches:", "같은 이름이 둘이면 모른다고 두기"),
    ("observation.py", '"restarts": target.get("restartCount"),', '"restarts": sum(c.get("restartCount", 0) for c in containers),', "컨테이너 재시작 횟수를 합산하지 않기"),
    ("observation.py", '((target.get("state") or {}).get("running") or {}).get("startedAt"),', 'max(c["state"]["running"]["startedAt"] for c in containers),', "가장 늦은 컨테이너 시작을 앱 시작으로 치지 않기"),
    ("observation.py", '((target.get("lastState") or {}).get("terminated") or {}).get("containerID")', "None", "끝난 컨테이너 ID 읽기"),
    ("observation.py", ', *(status.get("initContainerStatuses") or [])]', "]", "재시작 가능한 사이드카도 세기"),
    ("observation.py", '"other_restarts": sum(others) if all(type(r) is int for r in others) else None,', '"other_restarts": 0,', "다른 컨테이너 재시작 기록"),
    ("policy.py", '"role": w.role, "container": w.container}', '"role": w.role, "container": "app"}', "계획에 대상 컨테이너 결속"),
    ("policy.py", "if workload.container not in p.containers)", "if False)", "Pod에 없는 컨테이너 이름 거부"),
    ("cluster.py", 'containers=tuple(c["name"] for c in (item.get("spec") or {}).get("containers") or []),', "containers=(),", "Pod의 컨테이너 이름 읽기"),
    ("spec.py", 'if item["role"] != "app" and "container" in item:', "if False:", "앱 역할 밖의 컨테이너 이름 거부"),
    ("spec.py", '_text(item.get("container"), f"workload {name} container") if item["role"] == "app" else None', 'item.get("container")', "앱 역할은 컨테이너 이름 필수"),
    ("verdict.py", 'if _time(pod.get("container_started_at")) < _time(pod.get("started_at")) + ENFORCEMENT_MARGIN:', "if False:", "Pod가 정책에 묶인 뒤의 재시작인지"),
    ("verdict.py", "if running_since is None or running_since < policy_since + ENFORCEMENT_MARGIN:", "if False:", "정책 생성 시각과 재시작 대조"),
    ("verdict.py", "    if policy_since is None:\n", "    if False:\n", "정책 생성 시각을 모르면 판정 보류"),
    ("verdict.py", 'if any(p.get("host_network") is True for p in pods):', "if False:", "차단 뒤 hostNetwork Pod 확인"),
    ("verdict.py", "    except (AttributeError, KeyError, TypeError, ValueError):\n        # 관측", "    except ZeroDivisionError:\n        # 관측", "모양이 틀린 관측을 UNKNOWN으로"),
    ("verdict.py", "or (before_times and applied - min(before_times) > MAX_WINDOW)", "", "오래된 차단 전 측정 거부"),
    ("verdict.py", 'return sorted(rows, key=lambda r: _time(r.get("observed_at")))', 'return sorted(rows, key=lambda r: str(r.get("observed_at")))', "LSN 표본을 시간순으로"),
    ("verdict.py", "if not all(isinstance(p, dict) for p in pods[workload]):", "if False:", "알아볼 수 없는 Pod 항목 거부"),
    ("spec.py", 'if len(set(check_ids)) != len(check_ids):', "if False:", "새 연결 시험 이름 중복 거부"),
    ("cli.py", "    except Exception:  # noqa: BLE001", "    except ZeroDivisionError:  # noqa: BLE001", "처리 못 한 입력이 FAIL로 읽히지 않게"),
    ("verdict.py", "if restarts > (0 if record is None else 1):", "if False:", "시키지 않은 재시작 구분"),
    ("verdict.py", 'for field in ("started_at", "container_started_at")', 'for field in ("started_at",)', "컨테이너 재시작 뒤의 새 연결인지 확인"),
    ("observation.py", '"induced_restarts": induced_restarts or {},', '"induced_restarts": {},', "재시작 기록 전달"),
    ("verdict.py", 'return Check(check_id, FAIL, "SOURCE_PATH_OPEN")', 'return Check(check_id, PASS, "SOURCE_PATH_OPEN")', "열린 AWS 경로를 실패로"),
    ("verdict.py", "if not _controls_surround(route_id, control, rows, after):", "if False:", "대조군 확인"),
    ("verdict.py", 'if not _was_open(route_id, owner, before):\n        return Check(check_id, UNKNOWN, "BASELINE_NOT_OPEN")\n    if not _controls', "if not _controls", "차단 전 열림 확인"),
    ("verdict.py", 'DENIED_STATES = {"timeout", "refused", "unreachable"}', 'DENIED_STATES = {"timeout", "refused", "unreachable", "dns_failure"}', "이름 풀이 실패를 거부로 안 세기"),
    ("verdict.py", 'if {p.get("uid") for p in now} & {p.get("uid") for p in was}:', "if False:", "실제 재시작 확인"),
    ("verdict.py", 'if any(p.get("ready") is False for p in now):', "if False:", "준비 안 된 Pod를 실패로"),
    ("verdict.py", "if last <= first:", "if False:", "LSN이 안 움직이면 판정 보류"),
    ("verdict.py", 'first, last = _lsn(rows[0].get("lsn")), _lsn(rows[-1].get("lsn"))', 'first, last = rows[0].get("lsn"), rows[-1].get("lsn")', "LSN을 수로 비교"),
    ("verdict.py", 'return Check(check_id, FAIL, "REPLICATION_NOT_STREAMING")', 'return Check(check_id, PASS, "REPLICATION_NOT_STREAMING")', "끊긴 복제를 실패로"),
    ("verdict.py", 'if isinstance(cutover, dict) and cutover.get("writer") == "target":', "if True:", "쓰기 대상 확인"),
    ("verdict.py", "if _wider_than_plan(plan, after, readback[0]):", "if False:", "차단 뒤 합집합 재확인"),
    ("verdict.py", "if start != end:", "if False:", "측정 중 정책 변경 확인"),
    ("verdict.py", 'if any(p["metadata"]["namespace"] != plan["namespace"] for p in [*readback[0], *readback[1]]):', "if False:", "다른 네임스페이스의 정책 거부"),
    ("verdict.py", "if not plan_is_intact(plan):", "if False:", "손으로 고친 계획 판정 거부"),
    ("cli.py", "if verify and not plan_is_intact(plan):", "if False:", "손으로 고친 계획 사용 거부"),
    ("verdict.py", "or any(t is None or t < applied for t in after_times)", "", "차단 전 측정값 거부"),
    ("verdict.py", "verdict = FAIL if counts[FAIL] else UNKNOWN if counts[UNKNOWN] or not ordered else PASS", "verdict = FAIL if counts[FAIL] else PASS", "UNKNOWN을 통과로 안 세기"),
    ("verdict.py", "if newest is None or None in times or min(times) < newest:", "if False:", "재시작 뒤의 새 연결인지 확인"),
    ("verdict.py", 'if any(r.get("pod_uid") not in uids for r in rows):\n        return Check(check_id, UNKNOWN, "VANTAGE_NOT_BOUND")\n    states = {r.get("state") for r in rows}\n    if "connected" in states:', 'if "connected" in {r.get("state") for r in rows}:', "재는 Pod 신원 확인"),
    ("verdict.py", '*[Check(f"coverage:{d[\'id\']}", UNKNOWN, "UNMAPPED_DEPENDENCY") for d in plan["unmapped_dependencies"]],', "", "잴 수 없는 의존성 표시"),
    ("verdict.py", 'if any(start.get(m["metadata"]["name"]) != m["spec"] for m in plan["manifests"]):', "if False:", "적용된 정책이 계획과 같은지"),
    ("inventory_input.py", 'if doc.get("schema_version") != SUPPORTED_VERSION:', "if False:", "T16 버전 확인"),
    ("inventory_input.py", 'target.get("impl", "") if target.get("env_kind") == candidate_kind else ""', 'target.get("impl", "")', "다른 종류 후보의 목적지 구현 안 쓰기"),
    ("inventory_input.py", 'if source_kind == "aws":', "if True:", "AWS가 아닌 출발지 구분"),
    ("inventory_input.py", "    _verify_digest(doc)\n", "", "T16 해시 확인"),
    ("inventory_input.py", 'if isinstance(fit, dict) and fit.get("env_id") == candidate:', "if True:", "후보 환경 고르기"),
    ("residual_map.py", 'return "blocked_app_ok" if combined == "PASS" and drill_verdict == "PASS" else "unverified"', 'return "blocked_app_ok" if combined == "PASS" else "unverified"', "드릴 전체 통과 조건"),
    ("residual_map.py", 'if inventory.candidate != plan["candidate"]:', "if False:", "다른 후보로 지도 만들기 거부"),
    ("residual_map.py", 'status="broken" if combined == "FAIL" else "kept",', 'status="kept",', "끊긴 복제를 유지로 안 보이기"),
    ("residual_map.py", 'return "PASS" if all(v == "PASS" for v in values) else "UNKNOWN"', 'return "PASS"', "모르는 판정값을 통과로 안 치기"),
    ("residual_map.py", 'for name, w in sorted(plan["workloads"].items()) if w["role"] == "infrastructure"', 'for name, w in sorted(plan["workloads"].items()) if False', "차단 안 한 워크로드 표시"),
    ("residual_map.py", '    if dependency.replacement == "none":\n        return "remains"\n', "", "대체 없음은 남음"),
    ("residual_map.py", "if dependency.awaiting_review:", "if False:", "에이전트 확인 대기 표시"),
    ("residual_map.py", "*_residual_entries(inventory, plan),", "", "외부 서비스 표시"),
    ("residual_map.py", '*[_channel_entry(d, results) for d in plan["decisions"] if d["action"] == "KEEP"],', "", "남긴 복제 채널 표시"),
    ("residual_map.py", 'if report is not None and report.get("plan_digest") != plan["digest"]:', "if False:", "다른 시험의 판정 거부"),
    ("residual_map.py", 'if inventory.digest != plan["inventory_digest"]:', "if False:", "다른 인벤토리 거부"),
    ("probe.py", "connect((host, port), timeout=timeout).close()", 'connect((host, port), timeout=timeout).sendall(b"x")', "데이터를 보내지 않기"),
    ("probe.py", "if len(targets) > MAX_TARGETS:", "if False:", "대상 개수 제한"),
    ("probe.py", 'except socket.gaierror:\n        return "dns_failure"', 'except socket.gaierror:\n        return "refused"', "이름 풀이 실패 구분"),
    ("observation.py", "    return None\n\n\ndef _exit_blockers", "    return False\n\n\ndef _exit_blockers", "모르는 준비 상태를 모른다고 두기"),
    ("observation.py", 'if status.get("phase") in _FINISHED or metadata.get("deletionTimestamp"):', "if False:", "끝난 Pod 빼기"),
    ("observation.py", '"host_network": (item.get("spec") or {}).get("hostNetwork") is True,', '"host_network": False,', "hostNetwork 기록"),
    ("cli.py", "return exit_code(report)", "return 0", "판정을 종료 코드로 알리기"),
]


def fresh_copy(target: Path) -> None:
    for name in ("src", "tests", "profiles"):
        shutil.copytree(ROOT / name, target / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(ROOT / name, target / name)


def run_tests(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"],
        cwd=cwd, capture_output=True, text=True, timeout=180, check=False,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    out = parser.parse_args().out
    results = []
    with tempfile.TemporaryDirectory(prefix="t27-mutation-") as temp:
        work = Path(temp) / "project"
        fresh_copy(work)
        stale = [
            f"{file_name}: {what}"
            for file_name, old, _, what in MUTATIONS
            if (work / PACKAGE / file_name).read_text(encoding="utf-8").count(old) != 1
        ]
        if stale:
            raise SystemExit("mutation targets must appear exactly once:\n" + "\n".join(stale))
        baseline = run_tests(work)
        if baseline.returncode != 0:
            print(baseline.stdout[-2000:])
            raise SystemExit("unmodified copy must pass before mutating")
        for file_name, old, new, what in MUTATIONS:
            path = work / PACKAGE / file_name
            source = path.read_text(encoding="utf-8")
            path.write_text(source.replace(old, new), encoding="utf-8")
            done = run_tests(work)
            path.write_text(source, encoding="utf-8")
            failed = [line.split(" ")[1] for line in done.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))]
            results.append({"file": file_name, "removed": what, "caught": done.returncode != 0, "first_failure": failed[0] if failed else None})
            print(("caught  " if done.returncode else "SURVIVED"), file_name, what, flush=True)
    survived = [r for r in results if not r["caught"]]
    summary = {"baseline_passed": True, "total": len(results), "caught": len(results) - len(survived), "survived": survived, "results": results}
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{summary['caught']}/{summary['total']} caught")
    return 1 if survived else 0


if __name__ == "__main__":
    sys.exit(main())
