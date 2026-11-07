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


def test_demo_chat_baseline_is_unchanged_by_the_t46_catalog(repo, exports):
    # ADR-0018 4절 기준 결과. The SQS · Lambda catalog entries must not move any of it while the
    # demo app has no queue (request 11/8: the S1 screen stays the same until SQS is turned on).
    items = reconcile(scan_repo(repo, "app"), FileAdapter(exports).trace("chat"), "onprem")
    assert [(i.role, i.grade, i.found_in, i.agent_needed, i.target["impl"]) for i in items] == [
        ("db", "config_swap", "both", False, "cnpg"),
        ("iam", "adapter", "both", False, "k8s-rbac+openbao+seaweedfs-policy"),
        ("kms", "code_change", "deploy", True, "openbao-transit"),
        ("object_store", "config_swap", "both", False, "seaweedfs"),
        ("object_store", "config_swap", "deploy", True, "seaweedfs"),
        ("secrets", "code_change", "both", False, "openbao+eso"),
    ]


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
    # SNS shares the messaging role with SQS but ElasticMQ does not replace it (T46).
    (tmp_path / "q.py").write_text("import boto3\nsns = boto3.client('sns')\n", encoding="utf-8")
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


# T46 (ADR-0045 · 0046): SQS 표준 큐 → ElasticMQ, SQS 이벤트 소스 매핑 Lambda → 호환 런타임.
SENDER = "import boto3\nsqs = boto3.client('sqs')\nsqs.send_message(QueueUrl=URL, MessageBody='{}')\n"
COMPOSE = "services:\n  app:\n    environment:\n      AWS_ENDPOINT_URL_SQS: http://elasticmq:9324\n"
QUEUE_TF = 'resource "aws_sqs_queue" "checks" {\n  name = "message-checks"\n}\n'
FUNCTION_TF = (
    'resource "aws_lambda_function" "pii" {\n  function_name = "pii-check"\n}\n'
    'resource "aws_lambda_event_source_mapping" "pii" {\n  event_source_arn = aws_sqs_queue.checks.arn\n'
    "  function_name = aws_lambda_function.pii.arn\n  batch_size = 10\n}\n"
)


def _roles(tmp_path, files, kind="onprem"):
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    return {i.role: i for i in reconcile(scan_repo(tmp_path, "."), None, kind)}


def test_sqs_standard_queue_is_a_config_swap_to_elasticmq(tmp_path):
    msg = _roles(tmp_path, {"send.py": SENDER, "compose.yaml": COMPOSE})["messaging"]
    assert (msg.grade, msg.agent_needed, msg.target["impl"]) == ("config_swap", False, "elasticmq")
    assert "AWS_ENDPOINT_URL_SQS" in msg.rationale and "ADR-0046" in msg.target["verify"]
    # The endpoint variable is evidence of the queue, not an IAM item (request 11/8).
    assert {r["detail"]["name"] for r in msg.code_refs if r["kind"] == "config_env"} == {"AWS_ENDPOINT_URL_SQS"}


def test_sqs_endpoint_fixed_in_code_needs_a_code_change(tmp_path):
    body = "import boto3\nsqs = boto3.client('sqs', endpoint_url='https://sqs.ap-northeast-2.amazonaws.com')\n"
    assert _roles(tmp_path, {"send.py": body})["messaging"].grade == "code_change"


def test_sns_in_the_same_role_is_not_replaced_by_elasticmq(tmp_path):
    # Role default (ElasticMQ) must not speak for SNS: the item takes the heavier per-service answer.
    msg = _roles(tmp_path, {"send.py": SENDER + "sns = boto3.client('sns')\n"})["messaging"]
    assert msg.grade == "none" and msg.target["impl"] == ""
    assert msg.rationale.startswith("sns: 대체 없음")
    env_only = tmp_path / "env-only"
    env_only.mkdir()
    sns_env = "services:\n  app:\n    environment:\n      AWS_ENDPOINT_URL_SNS: http://sns:9911\n"
    assert _roles(env_only, {"compose.yaml": sns_env})["messaging"].grade == "none"


def test_sqs_fifo_stays_none(tmp_path):
    body = SENDER.replace("MessageBody='{}'", "MessageBody='{}', MessageGroupId='g'")
    roles = _roles(tmp_path, {"send.py": body, "queue.tf": QUEUE_TF, "fn.tf": FUNCTION_TF})
    assert roles["messaging"].grade == "none" and "FIFO" in roles["messaging"].rationale
    assert roles["messaging"].target["impl"] == ""  # no target to verify for what stays on AWS
    assert roles["function"].grade == "none"  # a Lambda on a FIFO queue is outside the runtime too


def test_lambda_on_an_sqs_event_source_mapping_is_an_adapter(tmp_path):
    roles = _roles(tmp_path, {"send.py": SENDER, "compose.yaml": COMPOSE, "queue.tf": QUEUE_TF, "fn.tf": FUNCTION_TF})
    fn = roles["function"]
    assert (fn.grade, fn.agent_needed, fn.target["impl"]) == ("adapter", False, "function-runtime")
    assert "ADR-0045" in fn.target["verify"]
    assert roles["messaging"].grade == "config_swap"
    assert "iam" not in roles  # nothing in this app is an IAM dependency


def test_lambda_outside_the_t46_scope_stays_none(tmp_path):
    base = {"send.py": SENDER, "queue.tf": QUEUE_TF}
    cases = {
        "no_mapping": {"fn.tf": 'resource "aws_lambda_function" "pii" {\n  function_name = "pii-check"\n}\n'},
        "push_trigger": {"fn.tf": FUNCTION_TF + 'resource "aws_lambda_permission" "api" {\n  principal = "apigateway.amazonaws.com"\n}\n'},
        "batch_failures": {"fn.tf": FUNCTION_TF.replace("batch_size = 10", 'function_response_types = ["ReportBatchItemFailures"]')},
        "filter": {"fn.tf": FUNCTION_TF.replace("batch_size = 10", "filter_criteria {\n    filter { pattern = \"x\" }\n  }")},
        "stream_source": {"fn.tf": FUNCTION_TF.replace("batch_size = 10", 'starting_position = "LATEST"')},
        "direct_invoke": {"fn.tf": FUNCTION_TF, "call.py": "import boto3\nboto3.client('lambda').invoke(FunctionName='f')\n"},
        "lambda_endpoint_env": {"fn.tf": FUNCTION_TF, "compose.yaml": "services:\n  app:\n    environment:\n      AWS_ENDPOINT_URL_LAMBDA: http://l:9001\n"},
    }
    for name, extra in cases.items():
        d = tmp_path / name
        d.mkdir()
        fn = _roles(d, {**base, **extra})["function"]
        assert (name, fn.grade, fn.grade_by, fn.agent_needed) == (name, "none", "rule", False)
        assert fn.target["impl"] == ""


def test_t46_entries_are_onprem_only(tmp_path):
    # GCP has no messaging/function entry yet: the rule still says it cannot decide.
    roles = _roles(tmp_path, {"send.py": SENDER, "queue.tf": QUEUE_TF, "fn.tf": FUNCTION_TF}, kind="gcp")
    assert roles["messaging"].grade == "unknown" and roles["function"].grade == "unknown"
