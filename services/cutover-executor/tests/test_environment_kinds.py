"""3×3 환경 종류 조합에서 Executor가 같은 계획을 만든다 (ADR-0034, A9).

Executor는 공통 기반(K8s · CNPG · PgBouncer)만 다루고 환경 종류(aws · gcp · onprem)를 몰라도 된다.
환경 ID와 종류를 9개 조합으로 바꿔도 컷오버 · 되돌리기가 기존 aws → onprem과 같은 단계 · 같은 원격 조작으로
끝나는지, 같은 종류끼리(다른 클러스터로의 이전)도 DB 자원이 다르면 받고 같으면 거부하는지 고정한다.
원격 대상은 test_runner_fakes의 가짜를 쓴다(실행 기록은 docker Postgres).
"""

import ast
import itertools
import json
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bravo_cutover.contract import CutoverRequest, Status
from bravo_cutover.rejections import SameResource
from bravo_cutover.runner import STEPS
from bravo_cutover.sites import Config, Settings, Site, registry_from_sites
from tests.test_runner_fakes import CONFIG, FakeCutover, after_cutover, make_world, site

KINDS = ("aws", "gcp", "onprem")
PAIRS = list(itertools.product(KINDS, KINDS))
PACKAGE = Path(__file__).resolve().parents[1] / "src" / "bravo_cutover"
CONTRACT = Path(__file__).resolve().parents[3] / "docs" / "contracts" / "environment-kinds.json"


def pair_config(source_kind: str | None, target_kind: str | None, *, same_resource: bool = False,
                source_env: str | None = None, target_env: str | None = None) -> Config:
    """출발지는 src 네임스페이스, 목적지는 dst 네임스페이스의 가짜 환경. 이름은 '<종류>-a', '<종류>-b'."""
    source_env = source_env or f"{source_kind}-a"
    target_env = target_env or f"{target_kind}-b"
    src = site(source_env, "src", "bravo_rev").model_copy(update={"kind": source_kind})
    dst = site(target_env, "dst", "bravo_fwd").model_copy(update={"kind": target_kind})
    if same_resource:
        dst = dst.model_copy(update={"resource_id": src.resource_id})
    return Config(settings=Settings(restore_verify_timeout_s=1), sites={source_env: src, target_env: dst},
                  origin=source_env)


def request(config: Config, source: str, target: str) -> CutoverRequest:
    return CutoverRequest(
        plan_id="plan-kinds", epoch=int(datetime.now(UTC).timestamp() * 1e6),
        source={"environment_id": source, "connection_ref": config.sites[source].connection_ref},
        target={"environment_id": target, "connection_ref": config.sites[target].connection_ref},
        write_pause_budget_ms=5000, deadline=datetime.now(UTC) + timedelta(minutes=1),
    )


def run(store, monkeypatch, config: Config, *, failback: bool = False):
    """컷오버(또는 컷오버가 끝난 상태에서 되돌리기)를 돌리고 (실행 기록, 원격 조작 횟수, 끝난 뒤 상태)를 돌려준다."""
    origin = config.origin
    [other] = [env for env in config.sites if env != origin]
    source, target = (other, origin) if failback else (origin, other)
    world = make_world(monkeypatch)
    if failback:
        after_cutover(world)
    req = request(config, source, target)
    execution_id = store.create(str(uuid.uuid4()), req, registry_from_sites(config.sites).resolve(req))[0].execution_id
    FakeCutover(store, world, config=config).run(execution_id)
    end = (world.login, world.sub, world.paused, world.ini_host, world.blocked, world.configmap)
    # 경로마다 동시에 보내는 조작(진입점 · 풀)이 있어 순서는 실행마다 다를 수 있다. 횟수로 비교한다.
    return store.get(execution_id), Counter(world.calls), end


@pytest.mark.parametrize(("source_kind", "target_kind"), PAIRS)
def test_every_kind_pair_cuts_over_and_fails_back_with_the_same_plan(store, monkeypatch, source_kind, target_kind):
    for failback in (False, True):
        baseline, baseline_calls, baseline_end = run(store, monkeypatch, CONFIG, failback=failback)
        config = pair_config(source_kind, target_kind)
        execution, calls, end = run(store, monkeypatch, config, failback=failback)

        assert baseline.status == execution.status == Status.DONE, execution.result
        succeeded = [s["step"] for s in execution.steps if s["status"] == "succeeded"]
        assert succeeded == [s["step"] for s in baseline.steps if s["status"] == "succeeded"] == list(STEPS)
        assert calls == baseline_calls and end == baseline_end

        receipt = execution.result["receipt"]
        source, target = f"{source_kind}-a", f"{target_kind}-b"
        expected = ("failback", target, source) if failback else ("cutover", source, target)
        assert (receipt["direction"], receipt["source"], receipt["target"]) == expected
        assert set(execution.result["path_acks_ms"]) == {
            f"{path}:{env}" for path in ("entry", "pool") for env in (source, target)}


@pytest.mark.parametrize(("source_kind", "target_kind"), PAIRS)
def test_distinct_db_resources_pass_and_a_shared_resource_is_rejected(source_kind, target_kind):
    """같은 종류끼리(aws → aws 등)는 다른 클러스터로의 이전이다. 두 DB 자원이 다르면 받고, 같으면 거부한다."""
    distinct = pair_config(source_kind, target_kind)
    source, target = f"{source_kind}-a", f"{target_kind}-b"
    assert registry_from_sites(distinct.sites).resolve(request(distinct, source, target)) == (
        distinct.sites[source].resource_id, distinct.sites[target].resource_id)
    assert registry_from_sites(distinct.sites).resolve(request(distinct, target, source))  # 되돌리기 방향
    assert distinct.deployment_registry().public_pair().source.environment_id == source

    shared = pair_config(source_kind, target_kind, same_resource=True)
    for a, b in ((source, target), (target, source)):
        with pytest.raises(SameResource):
            registry_from_sites(shared.sites).resolve(request(shared, a, b))
    with pytest.raises(ValueError, match="two distinct database resources"):
        shared.deployment_registry()


def test_same_environment_name_with_different_kind_labels_still_one_environment():
    """종류 표시는 환경을 구분하지 않는다. 같은 환경 ID를 두 번 등록할 수 없다(종류가 달라도)."""
    a = site("cluster-1", "src", "bravo_rev").model_copy(update={"kind": "aws"})
    with pytest.raises(ValueError, match="duplicate environment"):
        registry_from_sites({"x": a, "y": a.model_copy(update={"kind": "gcp", "resource_id": "other/chat"})})


@pytest.mark.parametrize("kinds", [(None, None), ("azure", "onprem"), ("gcp", None)])
def test_executor_does_not_need_to_know_the_kind(store, monkeypatch, kinds):
    """종류가 없거나 계약에 아직 없는 종류여도 같은 계획이다. 종류는 기록용이다."""
    baseline, baseline_calls, baseline_end = run(store, monkeypatch, CONFIG)
    config = pair_config(*kinds, source_env="site-one", target_env="site-two")
    execution, calls, end = run(store, monkeypatch, config)
    assert execution.status == Status.DONE, execution.result
    assert (calls, end) == (baseline_calls, baseline_end)
    assert "kind" not in json.dumps(execution.result)


def test_no_executor_module_reads_the_kind():
    """판정 · 실행 코드는 Site.kind를 읽지 않는다(필드 정의는 sites.py에만 있다)."""
    readers = []
    for path in PACKAGE.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Attribute) and node.attr == "kind":
                readers.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
            if isinstance(node, ast.Constant) and node.value == "kind" and path.name != "sites.py":
                readers.append(f"{path.relative_to(PACKAGE)}:{node.lineno}")
    allowed = {"prepare.py", "deploy.py"}  # Kubernetes 객체의 "kind" 칸(Cluster · Deployment · Job …)
    assert [r for r in readers if r.split(":")[0] not in allowed] == []


@pytest.mark.skipif(not CONTRACT.exists(), reason="environment-kinds.json 계약 없음")
def test_every_contract_kind_is_a_valid_site_kind():
    kinds = json.loads(CONTRACT.read_text())["kinds"]
    assert set(KINDS) <= set(kinds)
    for kind in kinds:
        assert Site.model_validate({**CONFIG.sites["env-aws"].model_dump(), "kind": kind}).kind == kind
