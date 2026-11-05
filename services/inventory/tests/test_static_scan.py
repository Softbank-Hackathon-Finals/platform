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


def test_query_string_credentials_never_reach_snippet_or_detail(tmp_path):
    # Review P1 (#144): tokens in a URL query must be masked in the snippet too; detail is already host-only.
    token = "FIXTURE-QUERY-TOKEN-5e1c"
    (tmp_path / "x.py").write_text(
        "import boto3\n"
        f'c = boto3.client("s3", endpoint_url="https://store.example.invalid/?access_token={token}")\n'
        f'URL = "https://example.execute-api.ap-northeast-2.amazonaws.com/private?access_token={token}"\n'
        f'SIGNED = "https://bucket.s3.amazonaws.com/k?X-Amz-Signature={token}&X-Amz-Credential=AKIA"\n'
        'PLAIN = "https://api.ap-northeast-2.amazonaws.com/health?verbose=1"\n',
        encoding="utf-8",
    )
    findings = scan_repo(tmp_path, ".")
    dump = json.dumps([f.as_dict() for f in findings])
    assert token not in dump
    sdk = [f for f in findings if f.kind == "sdk_call"][0]
    assert sdk.snippet.startswith("<masked") and sdk.detail["endpoint_url"] == "https://store.example.invalid"
    literals = [f for f in findings if f.kind == "endpoint_literal"]
    assert all("<masked" in f.snippet or "<query-removed>" in f.snippet for f in literals)
    plain = [f for f in literals if f.detail["value"] == "https://api.ap-northeast-2.amazonaws.com"][0]
    assert "verbose=1" not in plain.snippet and "?<query-removed>" in plain.snippet  # queries never reach the snippet


def test_escaped_string_literals_are_masked_by_their_decoded_value(tmp_path):
    # Review (#144, 류지환): the same URL written with Python escapes hides "://" and "?" from a
    # regex over the raw line; the decoded constant must drive the decision.
    token = "FIXTURE-ESC-TOKEN-71aa"
    (tmp_path / "x.py").write_text(
        "import boto3\n"
        f'c = boto3.client("s3", endpoint_url="https:\\x2f\\x2fu:{token}\\x40store.example.invalid")\n'
        f'URL = "https://api.ap-northeast-2.amazonaws.com/x\\x3faccess_token\\x3d{token}"\n'
        f'DSN = "postgresql\\u003a//chat:{token}@db/chat"\n',
        encoding="utf-8",
    )
    findings = scan_repo(tmp_path, ".")
    dump = json.dumps([f.as_dict() for f in findings])
    assert token not in dump and "x2f" not in dump
    sdk = [f for f in findings if f.kind == "sdk_call"][0]
    assert sdk.snippet.startswith("<masked") and sdk.detail["endpoint_url"] == "https://store.example.invalid"
    assert all(f.snippet.startswith("<masked") for f in findings if f.kind == "endpoint_literal")


def test_any_url_query_or_fragment_is_removed_from_snippets_without_a_name_list(tmp_path):
    # Review (#144, 이상무 2차): credentials under unlisted or percent-encoded query names.
    token = "FIXTURE-Q2-TOKEN-3c9e"
    (tmp_path / "x.py").write_text(
        "import boto3\n"
        f'c = boto3.client("s3", endpoint_url="https://store.example.invalid/?client_secret={token}")\n'
        f'URL = "https://api.ap-northeast-2.amazonaws.com/x?%61ccess_token={token}"\n'
        f'FRAG = "https://api.ap-northeast-2.amazonaws.com/y#token={token}"\n'
        f'ESC = "https://api.ap-northeast-2.amazonaws.com/z\\x3fcustom_param\\x3d{token}"\n',
        encoding="utf-8",
    )
    findings = scan_repo(tmp_path, ".")
    dump = json.dumps([f.as_dict() for f in findings])
    assert token not in dump
    for f in findings:
        if f.kind in {"sdk_call", "endpoint_literal"}:
            assert "?" not in f.snippet.replace("?<query-removed>", "") and "#" not in f.snippet.replace("#<fragment-removed>", "")
    sdk = [f for f in findings if f.kind == "sdk_call"][0]
    assert sdk.detail["endpoint_url"] == "https://store.example.invalid" and "store.example.invalid/?<query-removed>" in sdk.snippet
