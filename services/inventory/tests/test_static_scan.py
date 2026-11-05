import json

from bravo_inventory.static_scan import scan_repo


def test_finds_direct_sdk_calls_with_roles(repo):
    findings = scan_repo(repo, "app")
    calls = {(f.service, f.role, f.file) for f in findings if f.kind == "sdk_call"}
    assert ("secretsmanager", "secrets", "app/db_secret.py") in calls
    assert ("s3", "object_store", "app/storage.py") in calls


def test_s3_client_endpoint_is_recorded_as_configurable(repo):
    s3 = [f for f in scan_repo(repo, "app") if f.kind == "sdk_call" and f.service == "s3"][0]
    assert s3.detail["endpoint_url"] == "configurable"
    assert s3.line > 0 and "boto3.client" in s3.snippet


def test_env_refs_are_classified_by_prefix(repo):
    names = {f.detail["name"]: f.role for f in scan_repo(repo, "app") if f.kind == "env_ref"}
    assert names["DB_SECRET_ID"] == "secrets"
    assert names["S3_ENDPOINT_URL"] == "object_store"
    assert names["DB_HOST"] == "db"


def test_compose_env_lines_and_endpoints_are_found_but_values_masked(repo):
    cfg = [f for f in scan_repo(repo, "app") if f.kind == "config_env"]
    names = {f.detail["name"] for f in cfg}
    assert {"S3_ENDPOINT_URL", "AWS_SECRET_ACCESS_KEY", "DB_SECRET_ID"} <= names
    secret = [f for f in cfg if f.detail["name"] == "AWS_SECRET_ACCESS_KEY"][0]
    assert "local" not in secret.snippet and "masked" in secret.snippet


def test_parse_error_is_a_finding_not_a_crash(tmp_path):
    (tmp_path / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    findings = scan_repo(tmp_path, ".")
    assert [f.kind for f in findings] == ["parse_error"]


def test_other_cloud_and_region_literals(tmp_path):
    (tmp_path / "x.py").write_text(
        "from google.cloud import storage\nREGION = 'ap-northeast-2'\nURL = 'https://s3.ap-northeast-2.amazonaws.com'\n",
        encoding="utf-8",
    )
    kinds = {f.kind for f in scan_repo(tmp_path, ".")}
    assert {"other_cloud_import", "region_literal", "endpoint_literal"} <= kinds


def test_literal_endpoints_with_user_info_are_reduced_to_host(tmp_path):
    # Regression for review P1: endpoint literals carrying credentials must not reach the
    # inventory via snippet, detail.endpoint_url or detail.value.
    pw = "FIXTURE-PW-7c2d"
    (tmp_path / "x.py").write_text(
        "import boto3\n"
        f'c = boto3.client("s3", endpoint_url="http://AKIAFIXTURE0000000:{pw}@minio.local:9000")\n'
        f'URL = "https://user:{pw}@db.example.amazonaws.com/path?x=1"\n',
        encoding="utf-8",
    )
    (tmp_path / ".env.example").write_text(
        f"S3_ENDPOINT_URL=http://ak:{pw}@minio:9000\nAWS_REGION=ap-northeast-2\nAWS_SECRET_ACCESS_KEY={pw}\nS3_BUCKET=Mixed.Case\n",
        encoding="utf-8",
    )
    findings = scan_repo(tmp_path, ".")
    dump = json.dumps([f.as_dict() for f in findings])
    assert pw not in dump and "AKIAFIXTURE" not in dump
    by = {(f.kind, f.detail.get("name", f.service)): f for f in findings}
    assert by[("sdk_call", "s3")].detail["endpoint_url"] == "http://minio.local:9000"
    assert by[("sdk_call", "s3")].snippet.startswith("<masked")
    assert by[("endpoint_literal", "aws")].detail["value"] == "https://db.example.amazonaws.com"
    assert by[("config_env", "S3_ENDPOINT_URL")].detail["endpoint"] == "http://minio:9000"
    assert by[("config_env", "AWS_REGION")].detail["value"] == "ap-northeast-2"
    assert "value" not in by[("config_env", "S3_BUCKET")].detail  # not identifier-like → name only
    assert by[("config_env", "S3_BUCKET")].snippet == "S3_BUCKET: <masked>"
