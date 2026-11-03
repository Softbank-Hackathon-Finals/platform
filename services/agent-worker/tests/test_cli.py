import json
import os
import subprocess
import sys


def test_no_credentials_cli_stops_with_audit_and_preserves_existing_artifact(context, tmp_path):
    bundle, snapshot, _, _ = context
    bundle_path = tmp_path / "input.json"
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "output.json"
    bundle_path.write_text(bundle.model_dump_json())
    manifest_path.write_text(snapshot.manifest.model_dump_json())
    command = [sys.executable, "-m", "bravo_agent.cli", "analyze",
               "--bundle", str(bundle_path), "--manifest", str(manifest_path),
               "--snapshot", str(snapshot.root), "--output", str(output_path),
               "--model", "explicit-test-model"]
    environment = dict(os.environ)
    environment.pop("ANTHROPIC_API_KEY", None)
    environment.pop("OPENAI_API_KEY", None)
    result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "state=NEEDS_HUMAN" in result.stdout
    saved = output_path.read_bytes()
    artifact = json.loads(saved)
    assert artifact["proposal"] is None
    assert artifact["audit"][-1]["reason"] == "MODEL_OR_POLICY_FAILURE"
    again = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
    assert again.returncode == 2
    assert output_path.read_bytes() == saved


def test_schema_cli_is_offline_and_forbids_extra_fields():
    result = subprocess.run([sys.executable, "-m", "bravo_agent.cli", "schema"],
                            capture_output=True, text=True, check=True)
    schema = json.loads(result.stdout)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["edits"]["maxItems"] == 4
