import json

from bravo_inventory.deploy_trace import FileAdapter
from bravo_inventory.reconcile import reconcile
from bravo_inventory.static_scan import scan_repo


def _items(repo, exports, kind="onprem"):
    # first item per role: the role's main item (deploy-only resources split out come after it)
    out: dict = {}
    for i in reconcile(scan_repo(repo, "app"), FileAdapter(exports).trace("chat"), kind):
        out.setdefault(i.role, i)
    return out


def test_deploy_only_resource_inside_a_code_role_gets_its_own_item(repo, exports):
    # Real shakedown 2026-11-06: a bucket/secret granted to the workload but referenced by no
    # workload setting (fixture: legacy-audit bucket; real: ESO-synced control secret) was folded
    # into the role's `both` item and never flagged as a migration omission.
    items = reconcile(scan_repo(repo, "app"), FileAdapter(exports).trace("chat"), "onprem")
    s3 = [i for i in items if i.role == "object_store"]
    assert [i.found_in for i in s3] == ["both", "deploy"]
    main, extra = s3
    assert [r["name"] for r in main.deploy_refs] == ["demo-chat-attachments.example.invalid"]
    assert [r["name"] for r in extra.deploy_refs] == ["chat-audit-archive.example.invalid"]
    assert extra.risk == "migration_omission" and extra.agent_needed is True and extra.code_refs == []
    assert extra.grade == "config_swap" and extra.grade_by == "rule"  # catalog default for the role
    assert main.grade == "config_swap" and main.agent_needed is False
    assert len({i.id for i in items}) == len(items)


def test_demo_chat_grades_match_design_7_3(repo, exports):
    items = _items(repo, exports)
    assert items["object_store"].grade == "config_swap"
    assert items["secrets"].grade == "code_change"
    assert items["kms"].grade == "code_change" and items["kms"].found_in == "deploy"
    assert items["db"].grade == "config_swap"
    assert items["iam"].grade == "adapter"
    assert all(i.grade_by == "rule" for i in items.values())


def test_deploy_only_items_are_flagged_as_migration_omission(repo, exports):
    kms = _items(repo, exports)["kms"]
    assert kms.risk == "migration_omission" and "배포에만" in kms.rationale


def test_wildcard_policy_marks_iam_for_agent(repo, exports, tmp_path):
    for name in ("deployments.json", "serviceaccounts.json", "clusters.json"):
        (tmp_path / name).write_bytes((exports / name).read_bytes())
    (tmp_path / "aws.json").write_bytes((exports.parent.parent / "deploy-only-aws.json").read_bytes())
    items = _items(repo, tmp_path)
    assert items["iam"].agent_needed is True


def test_same_input_same_output(repo, exports):
    a = [i.as_dict() for i in reconcile(scan_repo(repo, "app"), FileAdapter(exports).trace("chat"), "onprem")]
    b = [i.as_dict() for i in reconcile(scan_repo(repo, "app"), FileAdapter(exports).trace("chat"), "onprem")]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_unknown_sdk_service_is_kept_and_marked(tmp_path, exports):
    (tmp_path / "q.py").write_text("import boto3\nsqs = boto3.client('sqs')\n", encoding="utf-8")
    items = reconcile(scan_repo(tmp_path, "."), None, "onprem")
    msg = [i for i in items if i.role == "messaging"][0]
    assert msg.grade == "none"


def _s3_items(tmp_path, files):
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    return [i for i in reconcile(scan_repo(tmp_path, "."), None, "onprem") if i.role == "object_store"][0]


def test_partially_configurable_s3_is_graded_conservatively(tmp_path):
    # Regression for review P2: one client without endpoint_url must not be hidden by another
    # that reads it from configuration.
    item = _s3_items(tmp_path, {
        "a.py": "import boto3, os\nc = boto3.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT_URL'))\n",
        "b.py": "import boto3\nlegacy = boto3.client('s3')\n",
    })
    assert item.grade == "code_change" and item.agent_needed is True
    assert "b.py:2" in item.rationale and "a.py" not in item.rationale


def test_all_fixed_s3_clients_need_code_change(tmp_path):
    item = _s3_items(tmp_path, {"b.py": "import boto3\nc = boto3.client('s3')\nd = boto3.client('s3', endpoint_url='https://s3.ap-northeast-2.amazonaws.com')\n"})
    assert item.grade == "code_change" and item.agent_needed is False
    assert "2건" in item.rationale


def test_all_configurable_s3_clients_are_config_swap(tmp_path):
    item = _s3_items(tmp_path, {"a.py": "import boto3, os\nc = boto3.client('s3', endpoint_url=os.environ['S3_ENDPOINT_URL'])\nd = boto3.client('s3', endpoint_url=os.getenv('S3_ENDPOINT_URL'))\n"})
    assert item.grade == "config_swap" and item.agent_needed is False
