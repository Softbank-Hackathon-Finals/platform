"""실제 실행 안내(docs/operations/t27-exit-drill-real-run.md)의 순서를 임시 k3s에서 돌린다.

    uv run python tests/integration/verify_real_run.py --out <폴더>

수동 실행이며 CI에 넣지 않는다. 앱 · DB · Tailscale · AWS는 verify_k3s.py의 대역이고 실제 환경에 접속하지 않는다.
풀러는 실제 이미지를, 복제 읽기는 실제 PostgreSQL을 따로 띄워 도구가 그 안에서 도는지 함께 잰다.
"""

import argparse
import copy
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE), str(ROOT / "scripts")]

import real_run  # noqa: E402
import verify_k3s as lab  # noqa: E402

from bravo_exit_drill import verdict as verdict_module  # noqa: E402
from bravo_exit_drill.aws_endpoints import endpoint_host  # noqa: E402
from bravo_exit_drill.cluster import parse_snapshot  # noqa: E402
from bravo_exit_drill.control_pod import render_control_pod  # noqa: E402
from bravo_exit_drill.inventory_input import parse_inventory  # noqa: E402
from bravo_exit_drill.policy import build_plan  # noqa: E402
from bravo_exit_drill.residual_map import build_map, render_markdown  # noqa: E402
from bravo_exit_drill.spec import parse_spec  # noqa: E402
from bravo_exit_drill.verdict import judge  # noqa: E402

KUBECTL = f"docker exec -i {lab.K3S} kubectl"
ROLLBACK_LABEL = "app.kubernetes.io/part-of=bravo-exit-drill"
POOLER_IMAGE = "ghcr.io/cloudnative-pg/pgbouncer:1.25.1@sha256:e6ddfe22d845e603825e235dd8334b21ecd125abea2a2172478f556b8dee2bb8"
POSTGRES_IMAGE = "mirror.gcr.io/library/postgres:18-alpine"
POOLER_LAB = "t27-real-pooler"
POSTGRES_LAB = "t27-real-postgres"
PSA_NAMESPACE = "t27-psa"
NO_HOST = "10.127.27.250"


def timed(results: dict, name: str, started: float) -> None:
    results["seconds"][name] = round(time.monotonic() - started, 1)


def pinned_control_image() -> str:
    """대조군은 digest로 고정한 이미지만 받는다. 실제 장비처럼 digest 별칭을 붙여 쓴다."""
    reference = lab.run("docker", "image", "inspect", lab.PY_IMAGE, "--format", "{{index .RepoDigests 0}}").strip()
    digest = reference.split("@")[1]
    image = f"docker.io/library/python@{digest}"
    lab.run("docker", "exec", lab.K3S, "ctr", "-n", "k8s.io", "images", "tag", "--force", f"docker.io/library/{lab.PY_IMAGE}", image)
    return image


def start_control(spec, image: str) -> None:
    pod = render_control_pod(spec, image)
    lab.apply(pod)
    lab.kubectl("-n", lab.NS, "wait", "--for=condition=Ready", f"pod/{pod['metadata']['name']}", "--timeout=60s", timeout=80)


def psa_accepts_control(spec, image: str) -> bool:
    """target-dev는 restricted다. 대역 Pod가 그 기준을 못 맞춰 별도 네임스페이스에서 서버 검사로 잰다."""
    lab.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": PSA_NAMESPACE, "labels": {
        "pod-security.kubernetes.io/enforce": "restricted", "pod-security.kubernetes.io/enforce-version": "v1.35",
    }}})
    pod = render_control_pod(spec, image)
    pod["metadata"]["namespace"] = PSA_NAMESPACE
    done = subprocess.run(
        ["docker", "exec", "-i", lab.K3S, "kubectl", "apply", "--dry-run=server", "-f", "-"],
        input=json.dumps(pod), capture_output=True, text=True, timeout=60, check=False,
    )
    return done.returncode == 0


def stage_map(plan: dict, inventory, report) -> dict:
    return build_map(inventory, plan, report)


def drill(plan: dict, kube, *, cutover: dict | None) -> tuple[dict, dict, dict]:
    """안내 문서 4단계와 같은 순서."""
    read_replication = lambda: lab.stream_samples(plan)  # noqa: E731 - 대역 DB에는 psql이 없어 받는 쪽에서 읽는다.
    before = real_run.collect(kube, plan, "before", read_replication=read_replication)
    time.sleep(1)
    applied_at = real_run.clock(kube, plan)
    lab.apply(*copy.deepcopy(plan["manifests"]))
    time.sleep(lab.SETTLE_S)
    lab.restart("demo-chat", "target-pgbouncer")
    time.sleep(lab.SETTLE_S)
    induced = real_run.restart(kube, plan, ["app", "pooler"])
    time.sleep(lab.SETTLE_S)
    after = real_run.collect(kube, plan, "after", applied_at=applied_at, cutover=cutover, induced=induced, read_replication=read_replication)
    return before, after, judge(plan, before, after)


def rollback(plan: dict, kube, aws_route: str) -> dict:
    """안내 문서의 되돌리기 명령 그대로."""
    lab.kubectl("-n", lab.NS, "delete", "networkpolicy,pod", "-l", ROLLBACK_LABEL, "--ignore-not-found", timeout=120)
    time.sleep(lab.SETTLE_S)
    left = json.loads(lab.kubectl("get", "networkpolicy,pod", "-n", lab.NS, "-l", ROLLBACK_LABEL, "-o", "json"))["items"]
    run = real_run.measure(kube, plan, "app")
    state = next((r["state"] for r in run["results"] if r.get("route_id") == aws_route), None) if run else None
    return {"objects_left": len(left), "aws_state_after_rollback": state, "stream_alive": lab.stream_alive()}


def make_compatible() -> None:
    """T30 렌더러가 만드는 호환 구성의 표지와 환경 변수를 대역 앱에 준다(ADR-0017)."""
    lab.kubectl("-n", lab.NS, "patch", "deployment", "demo-chat", "--type=strategic", "-p", json.dumps({"spec": {"template": {
        "metadata": {"annotations": {"bravo.t30/not-exit-evidence": "secrets-manager-compat-sidecar"}},
        "spec": {"containers": [{"name": "app", "env": [{"name": "AWS_ENDPOINT_URL_SECRETS_MANAGER", "value": "http://127.0.0.1:18081"}]}]},
    }}}))
    lab.kubectl("-n", lab.NS, "rollout", "status", "deployment/demo-chat", "--timeout=120s", timeout=140)


def real_pooler_image(aws_ip: str) -> dict:
    """실제 풀러 이미지 안에서 bash 접속 시험과 컨테이너 종료 명령이 도는지 잰다. 이미지의 진입점을 그대로 쓴다."""
    ini = "[databases]\nx = host=127.0.0.1 port=1 dbname=x\n[pgbouncer]\nlisten_addr = 127.0.0.1\nlisten_port = 6432\nauth_type = any\nunix_socket_dir =\n"
    lab.docker_remove(POOLER_LAB)
    with tempfile.TemporaryDirectory(prefix="t27-pooler-") as directory:
        config = Path(directory) / "pgbouncer.ini"
        config.write_text(ini, encoding="utf-8")
        config.chmod(0o644)
        lab.run("docker", "run", "-d", "--name", lab.guard(POOLER_LAB), "--network", lab.NETWORK, "--label", "t27=1", "-v", f"{config}:/etc/pgbouncer/pgbouncer.ini:ro", POOLER_IMAGE)
        try:
            time.sleep(2)
            output = lab.run(
                "docker", "exec", POOLER_LAB, "bash", "-c", real_run.SHELL_PROBE, "bash", "2",
                "tcp", aws_ip, "443", "tcp", aws_ip, "1", "tcp", NO_HOST, "5432", "tcp", "no-such-host.invalid", "1", "dns", "no-such-host.invalid", "0",
            )
            running = lambda: lab.run("docker", "inspect", POOLER_LAB, "--format", "{{.State.Running}}").strip() == "true"  # noqa: E731
            at_start = running()
            lab.run("docker", "exec", POOLER_LAB, "sh", "-c", "kill -TERM 1", check=False)
            time.sleep(2)
            after_pid_one_only = running()
            lab.run("docker", "exec", POOLER_LAB, "sh", "-c", real_run.STOP_CONTAINER, check=False)
            time.sleep(2)
            return {
                "shell_probe_states": [line.split(" ")[0] for line in output.splitlines()],
                "running_at_start": at_start, "running_after_kill_pid_one": after_pid_one_only, "running_after_stop_container": running(),
            }
        finally:
            lab.docker_remove(POOLER_LAB)


def real_postgres_read() -> dict:
    """복제 상태 읽기를 실제 psql과 walsender로 잰다. 물리 복제 클라이언트지만 읽는 표는 같다."""
    lab.docker_remove(POSTGRES_LAB)
    lab.run("docker", "run", "-d", "--name", lab.guard(POSTGRES_LAB), "--label", "t27=1", "-e", "POSTGRES_HOST_AUTH_METHOD=trust", "-e", "POSTGRES_DB=chat", POSTGRES_IMAGE, timeout=240)
    exec_ = ("docker", "exec", POSTGRES_LAB)
    try:
        lab.wait_until(lambda: lab.run(*exec_, "pg_isready", "-U", "postgres", "-d", "chat", check=False) != "", 60, "postgres")
        query = [real_run.replication_query(real_run.REVERSE_APPLICATION_NAME)[0], "-U", "postgres", *real_run.replication_query(real_run.REVERSE_APPLICATION_NAME)[1:]]
        absent = real_run.parse_replication(lab.run(*exec_, *query))
        lab.run("docker", "exec", "-d", "-e", f"PGAPPNAME={real_run.REVERSE_APPLICATION_NAME}", POSTGRES_LAB, "sh", "-c", "mkdir -p /tmp/wal && pg_receivewal -D /tmp/wal -U postgres -h 127.0.0.1")
        lab.wait_until(lambda: real_run.parse_replication(lab.run(*exec_, *query))["state"] == "streaming", 30, "walsender")
        first = real_run.parse_replication(lab.run(*exec_, *query))
        lab.run(*exec_, "psql", "-U", "postgres", "-d", "chat", "-c", "CREATE TABLE IF NOT EXISTS t(i int); INSERT INTO t SELECT generate_series(1, 1000)")
        time.sleep(2)
        second = real_run.parse_replication(lab.run(*exec_, *query))
        refused = lab.run(*exec_, "psql", "-U", "postgres", "-d", "chat", "-v", "ON_ERROR_STOP=1", "-c", "SET default_transaction_read_only=on", "-c", "CREATE TABLE should_fail(i int)", check=False) == ""
        return {"no_subscriber": absent, "streaming": first["state"], "lsn_moved": first["lsn"] != second["lsn"], "read_only_session_refused_write": refused}
    finally:
        lab.docker_remove(POSTGRES_LAB)


def source_manifest() -> dict:
    """측정에 쓴 소스를 커밋으로 식별한다. 커밋과 다른 작업 트리로 잰 값을 그 커밋의 증거로 적지 않는다."""
    paths = [
        str(p.relative_to(lab.REPO))
        for p in [*sorted((ROOT / "src/bravo_exit_drill").glob("*.py")), ROOT / "scripts/real_run.py", HERE / "standin.py", HERE / "verify_k3s.py", Path(__file__).resolve()]
    ]
    if lab.run("git", "-C", str(lab.REPO), "status", "--porcelain", "--", *paths).strip():
        raise RuntimeError("commit the measured sources first")
    revision = lab.run("git", "-C", str(lab.REPO), "rev-parse", "HEAD").strip()
    return json.loads(lab.run("python3", str(lab.REPO / "scripts/evidence_sources.py"), "--repo", str(lab.REPO), "create", "--revision", revision, *paths))


def run_all(out: Path) -> int:
    sources = source_manifest()
    inventory_doc = json.loads((ROOT / "tests/fixtures/inventory-demo-chat.json").read_text(encoding="utf-8"))
    spec_doc = json.loads((ROOT / "profiles/target-dev.json").read_text(encoding="utf-8"))
    spec = parse_spec(spec_doc)
    inventory = parse_inventory(inventory_doc, candidate="onprem-demo")
    hosts = sorted({endpoint_host(s, spec.aws_region) for d in inventory.dependencies for s in d.aws_services})
    kube = real_run.kubectl(KUBECTL)
    results: dict = {"seconds": {}, "eligibility_code_present": hasattr(verdict_module, "_check_eligibility")}

    lab.docker_remove(lab.K3S, lab.AWS, lab.SOURCE)
    lab.run("docker", "network", "rm", lab.guard(lab.NETWORK), check=False)
    lab.run("docker", "network", "create", "--subnet", lab.SUBNET, "--label", "t27=1", lab.guard(lab.NETWORK))
    try:
        lab.container(lab.AWS, {"ROLE": "tcp", "PORTS": "443"}, lab.AWS_IP)
        lab.container(lab.SOURCE, {"ROLE": "source", "REVERSE_HOST": lab.K3S_IP, "REVERSE_PORT": lab.NODE_PORT}, lab.SOURCE_IP)
        lab.start_k3s()
        lab.install_workloads(hosts)
        results["real_pooler_image"] = real_pooler_image(lab.AWS_IP)
        print("pooler image:", results["real_pooler_image"], flush=True)
        results["real_postgres_read"] = real_postgres_read()
        print("postgres read:", results["real_postgres_read"], flush=True)

        # 안내 문서의 대조군을 쓴다. 장치가 깔아 둔 대역 대조군은 지운다.
        lab.kubectl("-n", lab.NS, "delete", "deployment", "exit-drill-control", "--ignore-not-found", "--wait=true", timeout=120)
        lab.wait_until(lambda: not lab.pod_of("bravo-exit-drill-control=true"), 60, "stand-in control gone")
        image = pinned_control_image()
        results["control_pod_accepted_by_restricted_psa"] = psa_accepts_control(spec, image)

        # 1단계: 대조군 → 스냅샷 → 계획 → 차단 없는 지도
        started = time.monotonic()
        start_control(spec, image)
        plan = build_plan(spec, inventory, parse_snapshot(lab.get_json("pods,networkpolicies"), namespace=lab.NS))
        lab.check_no_real_aws(plan, hosts)
        no_drill = stage_map(plan, inventory, None)
        timed(results, "plan_and_map_without_block", started)
        results["map_without_block"] = {"drill_verdict": no_drill["drill_verdict"], "status": no_drill["summary"]["status"]}
        lab.wait_until(lab.stream_alive, 60, "replication stream")

        # 2단계: 앱이 AWS에서 벗어난 경우. 쓰기 대상을 확인한 입력을 준다.
        started = time.monotonic()
        before, after, report = drill(plan, kube, cutover={"writer": "target", "note": "stand-in"})
        timed(results, "full_drill", started)
        passed = stage_map(plan, inventory, report)
        results["full_drill"] = {
            "verdict": report["verdict"], "counts": report["summary"], "not_pass": passed["drill_not_pass"], "status": passed["summary"]["status"],
            "app_restarts": [{"app": p["restarts"], "others": p["other_restarts"]} for p in after["pods"]["app"]],
            "shell_vantages_measured": sorted({p["vantage"] for p in after["probes"]} & real_run.SHELL_VANTAGES),
        }
        print("full drill:", results["full_drill"], flush=True)
        started = time.monotonic()
        results["rollback"] = rollback(plan, kube, f"aws-{inventory.dependencies[-1].aws_services[0]}")
        timed(results, "rollback", started)
        print("rollback:", results["rollback"], flush=True)

        # 3단계: 쓰기 대상을 확인하지 못한 입력. 지어내지 않으면 통과하지 않는다.
        start_control(spec, image)
        _, _, unconfirmed = drill(plan, kube, cutover=None)
        results["without_cutover_input"] = {"verdict": unconfirmed["verdict"], "not_pass": stage_map(plan, inventory, unconfirmed)["drill_not_pass"]}
        print("no cutover input:", results["without_cutover_input"], flush=True)
        rollback(plan, kube, f"aws-{inventory.dependencies[-1].aws_services[0]}")

        # 4단계: 호환 구성. 비자격 판정은 PR #210에 있다. 없으면 재지 못한 것으로 남긴다.
        make_compatible()
        start_control(spec, image)
        started = time.monotonic()
        _, _, compat = drill(plan, kube, cutover={"writer": "target", "note": "stand-in"})
        timed(results, "compat_drill", started)
        compat_map = stage_map(plan, inventory, compat)
        results["compat_pod"] = {
            "verdict": compat["verdict"], "not_pass": compat_map["drill_not_pass"], "drill_verdict_on_map": compat_map["drill_verdict"], "status": compat_map["summary"]["status"],
        }
        print("compat:", results["compat_pod"], flush=True)
        rollback(plan, kube, f"aws-{inventory.dependencies[-1].aws_services[0]}")

        full_ok = report["verdict"] == "PASS" and "blocked_app_ok" in passed["summary"]["status"]
        back_ok = results["rollback"]["objects_left"] == 0 and results["rollback"]["aws_state_after_rollback"] == "connected" and results["rollback"]["stream_alive"]
        cutover_ok = any(c["code"] == "CUTOVER_NOT_CONFIRMED" for c in results["without_cutover_input"]["not_pass"]) and unconfirmed["verdict"] == "UNKNOWN"
        tools_ok = (
            results["real_pooler_image"]["shell_probe_states"][:2] == ["connected", "refused"]
            and results["real_pooler_image"]["running_after_kill_pid_one"] is True
            and results["real_pooler_image"]["running_after_stop_container"] is False
            and results["real_postgres_read"]["streaming"] == "streaming"
            and results["real_postgres_read"]["lsn_moved"] is True
            and results["real_postgres_read"]["read_only_session_refused_write"] is True
            and results["control_pod_accepted_by_restricted_psa"] is True
        )
        if results["eligibility_code_present"]:
            compat_ok = compat["verdict"] == "UNKNOWN" and "blocked_app_ok" not in compat_map["summary"]["status"] and any(c["code"] == "NOT_EXIT_EVIDENCE" for c in compat_map["drill_not_pass"])
        else:
            compat_ok = True  # 판정 코드가 없는 브랜치에서는 이 결과로 합격을 말하지 않는다.
            results["compat_pod"]["not_judged_without_pr_210"] = True
        results["all_as_expected"] = full_ok and back_ok and cutover_ok and tools_ok and compat_ok
        results["python_image"] = lab.PY_IMAGE
        results["k3s_image"] = lab.K3S_IMAGE
        results["pooler_image"] = POOLER_IMAGE
        results["postgres_image"] = POSTGRES_IMAGE
        results.update(sources)
        out.mkdir(parents=True, exist_ok=True)
        (out / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        (out / "residual-map-without-block.md").write_text(render_markdown(no_drill) + "\n", encoding="utf-8")
        (out / "residual-map-compat.md").write_text(render_markdown(compat_map) + "\n", encoding="utf-8")
        print("all as expected:", results["all_as_expected"], flush=True)
        return 0 if results["all_as_expected"] else 1
    finally:
        lab.docker_remove(lab.K3S, lab.AWS, lab.SOURCE, POOLER_LAB, POSTGRES_LAB)
        lab.run("docker", "network", "rm", lab.guard(lab.NETWORK), check=False)
        print("left containers:", lab.run("docker", "ps", "-a", "--filter", "name=^t27-", "--format", "{{.Names}}", check=False).split(), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    return run_all(parser.parse_args().out)


if __name__ == "__main__":
    sys.exit(main())
