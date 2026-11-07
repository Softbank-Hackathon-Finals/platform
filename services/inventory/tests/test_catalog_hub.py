"""ADR-0034 target.catalog: role hub for every (source kind, target kind) pair of aws · gcp · onprem."""

import itertools
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bravo_inventory import build_inventory, validate_inventory
from bravo_inventory.capabilities import KIND_CAPABILITIES, load_profile
from bravo_inventory.catalog import CATALOG, GRADES, KINDS, PAIR_EXCEPTIONS, ROLES, target_entry
from bravo_inventory.deploy_trace import FileAdapter
from bravo_inventory.grading import Item, _env_names, apply_pair, grade_role
from bravo_inventory.reconcile import reconcile
from bravo_inventory.static_scan import scan_repo

PAIRS = list(itertools.product(KINDS, KINDS))
CONTRACT = Path(__file__).resolve().parents[3] / "docs/contracts/environment-kinds.json"
PROFILES = Path(__file__).parent / "fixtures/profiles"


def test_kinds_match_the_environment_kind_contract():
    assert set(KINDS) == set(json.loads(CONTRACT.read_text(encoding="utf-8"))["kinds"])
    assert set(CATALOG) == set(KINDS) == set(KIND_CAPABILITIES)


@pytest.mark.parametrize("kind", KINDS)
def test_every_target_kind_answers_the_same_roles(kind):
    assert set(CATALOG[kind]) == set(ROLES) == set(CATALOG["onprem"])


@pytest.mark.parametrize("source, target", PAIRS)
@pytest.mark.parametrize("role", ROLES)
def test_every_pair_and_role_has_an_entry(source, target, role):
    entry = target_entry(source, target, role)
    assert entry is not None
    assert entry["grade"] in GRADES and entry["grade"] != "unknown"
    assert entry["verify"] and entry["basis"] in {"same_kind", "pair_exception", "target_default"}
    assert (entry["source_kind"], entry["target_kind"]) == (source, target)
    if entry["grade"] != "none":
        assert entry["impl"], "a replacement needs an implementation"


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("role", ROLES)
def test_same_kind_is_config_swap(kind, role):
    entry = target_entry(kind, kind, role)
    assert entry["grade"] == "config_swap" and entry["basis"] == "same_kind" and entry["impl"]
    assert kind in entry["reason"]


@pytest.mark.parametrize("role", ROLES)
def test_aws_to_onprem_hub_equals_the_legacy_lookup(role):
    legacy = target_entry("onprem", role)
    entry = target_entry("aws", "onprem", role)
    assert entry["basis"] == "target_default"
    assert {k: entry[k] for k in ("impl", "grade", "verify")} == legacy == CATALOG["onprem"][role]


def test_exceptions_are_cross_kind_and_point_at_known_roles():
    for (source, target, role), exc in PAIR_EXCEPTIONS.items():
        assert source != target and source in KINDS and target in KINDS and role in ROLES
        assert exc["grade"] in GRADES and exc["reason"]
        assert target_entry(source, target, role)["basis"] == "pair_exception"


def test_aws_target_catalog_names_the_aws_services():
    aws = CATALOG["aws"]
    assert (aws["runtime"]["impl"], aws["object_store"]["impl"], aws["kms"]["impl"], aws["iam"]["impl"]) == ("eks", "s3", "kms", "irsa")
    assert aws["secrets"]["impl"] == "secrets-manager+eso" and aws["db"]["impl"] == "cnpg"


def test_legacy_two_argument_lookup_is_kept():
    assert target_entry("onprem", "db") == CATALOG["onprem"]["db"]
    assert target_entry("nowhere", "db") is None
    with pytest.raises(TypeError):
        target_entry("onprem")


# --- grading rationale comes from the catalog ---------------------------------------------------

def test_iam_rationale_names_the_target_not_onprem_rbac():
    assert "RBAC" in grade_role("iam", [], set(), "onprem")[1]  # unchanged aws→onprem wording
    aws = grade_role("iam", [], set(), "aws", "gcp")
    gcp = grade_role("iam", [], set(), "gcp", "aws")
    assert aws[0] == gcp[0] == "adapter"
    assert "IRSA" in aws[1] and "RBAC" not in aws[1]
    assert "Workload Identity" in gcp[1] and "RBAC" not in gcp[1]


def test_cloud_target_messaging_is_a_code_change_not_unknown():
    grade, text, agent = grade_role("messaging", [], set(), "aws", "gcp")
    assert (grade, agent) == ("code_change", False) and "sqs" in text
    assert grade_role("messaging", [], set(), "onprem", "aws")[0] == "none"  # onprem unchanged


# --- the pair pass over reconciled items ---------------------------------------------------------

def _reconciled(repo, exports, target):
    findings = scan_repo(repo, "app")
    return findings, reconcile(findings, FileAdapter(exports).trace("chat"), target)


def test_apply_pair_leaves_aws_to_onprem_untouched(repo, exports):
    findings, items = _reconciled(repo, exports, "onprem")
    before = json.dumps([i.as_dict() for i in items], sort_keys=True, ensure_ascii=False)
    after = apply_pair(items, "aws", "onprem", _env_names(findings))
    assert json.dumps([i.as_dict() for i in after], sort_keys=True, ensure_ascii=False) == before


def test_same_kind_items_are_config_swap(repo, exports):
    findings, items = _reconciled(repo, exports, "aws")
    items = apply_pair(items, "aws", "aws", _env_names(findings))
    graded = [i for i in items if i.role != "unknown"]
    assert graded and all(i.grade == "config_swap" for i in graded)
    assert all(i.target["basis"] == "same_kind" and i.target["source_kind"] == "aws" for i in graded)
    # deploy-only items stay flagged for the agent: the resource still has to exist in the new account
    assert all(i.agent_needed for i in graded if i.risk == "migration_omission")


def test_onprem_source_object_store_is_config_swap_even_without_endpoint_in_code(tmp_path):
    # On an onprem source an S3 client without endpoint_url can only reach SeaweedFS through
    # configuration (AWS_ENDPOINT_URL), so moving to S3 is a settings change.
    (tmp_path / "b.py").write_text("import boto3\nc = boto3.client('s3')\n", encoding="utf-8")
    findings = scan_repo(tmp_path, ".")
    raw = [i for i in reconcile(findings, None, "aws") if i.role == "object_store"][0]
    assert raw.grade == "code_change"  # target default rule
    item = apply_pair([raw], "onprem", "aws", _env_names(findings))[0]
    assert item.grade == "config_swap" and "SeaweedFS" in item.rationale and item.target["basis"] == "pair_exception"


def test_gcp_source_deploy_only_bucket_takes_the_pair_default():
    item = Item("item-001", "object_store", "object_store", "deploy", "config_swap", "rule", "배포에만 있음", True, risk="migration_omission")
    apply_pair([item], "gcp", "onprem", set())
    assert item.grade == "code_change" and "GCS" in item.rationale and item.agent_needed


def test_agent_flag_is_never_cleared_by_the_pair_pass(tmp_path):
    (tmp_path / "a.py").write_text("import boto3, os\nc = boto3.client('s3', endpoint_url=os.environ['S3_ENDPOINT_URL'])\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("import boto3\nlegacy = boto3.client('s3')\n", encoding="utf-8")
    findings = scan_repo(tmp_path, ".")
    raw = [i for i in reconcile(findings, None, "aws") if i.role == "object_store"][0]
    assert raw.agent_needed
    item = apply_pair([raw], "aws", "aws", _env_names(findings))[0]
    assert item.grade == "config_swap" and item.agent_needed and "규칙 메모" in item.rationale


# --- capabilities and the document for all nine pairs -------------------------------------------

def test_aws_and_gcp_profiles_load():
    for name, kind in (("aws-eks.json", "aws"), ("gcp-gke.json", "gcp")):
        assert load_profile(PROFILES / name)["env_kind"] == kind


def _profile(tmp_path: Path, kind: str) -> Path:
    path = tmp_path / f"{kind}-target.json"
    provides = {"postgres_operator": "cnpg"} | ({"s3_compatible": "seaweedfs", "secret_store": "openbao"} if kind == "onprem" else {})
    path.write_text(json.dumps({"env_id": f"{kind}-target", "env_kind": kind, "provides": provides}), encoding="utf-8")
    return path


@pytest.mark.parametrize("source, target", PAIRS)
def test_document_for_every_pair_validates(repo, exports, tmp_path, source, target):
    doc = build_inventory(
        app_id="demo-chat", repo=repo, app_path="app",
        source_env={"env_id": f"{source}-source", "env_kind": source, "namespace": "chat"},
        adapter=FileAdapter(exports), profiles=[_profile(tmp_path, target)], now=datetime(2026, 11, 7, tzinfo=UTC),
    )
    validate_inventory(doc)
    cand = doc["candidates"][0]
    assert cand["env_kind"] == target and cand["source_kind"] == source
    assert cand["migration"] == ("same_kind_cluster" if source == target else "cross_kind")
    assert cand["missing_capabilities"] == []  # native managed services + declared CNPG cover the checked roles
    if source == target:
        assert {i["grade"] for i in doc["items"] if i["role"] != "unknown"} == {"config_swap"}
        assert cand["verdict"] == "fits"


def test_aws_target_capabilities_are_native(repo, exports):
    doc = build_inventory(app_id="demo-chat", repo=repo, app_path="app",
                          source_env={"env_id": "onprem-src", "env_kind": "onprem", "namespace": "chat"},
                          adapter=FileAdapter(exports), profiles=[PROFILES / "aws-eks.json"])
    caps = doc["candidates"][0]["capabilities"]
    assert caps["s3_compatible"] == {"value": "s3", "source": "native: aws"}
    assert caps["postgres_operator"]["source"] == "declared: profile"


def test_report_without_profiles_uses_the_named_target_kind(repo):
    doc = build_inventory(app_id="demo-chat", repo=repo, app_path="app",
                          source_env={"env_id": "gcp-src", "env_kind": "gcp", "namespace": "chat"},
                          adapter=None, profiles=[], target_kind="aws")
    assert {i["target"]["env_kind"] for i in doc["items"]} == {"aws"}
    with pytest.raises(ValueError):
        build_inventory(app_id="demo-chat", repo=repo, app_path="app",
                        source_env={"env_id": "gcp-src", "env_kind": "gcp", "namespace": "chat"},
                        adapter=None, profiles=[], target_kind="azure")
