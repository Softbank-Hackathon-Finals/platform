"""Historical proof labels must be bound before signing local qualification input."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "retained_source_qualification",
    Path(__file__).resolve().parents[1] / "testing/verify_live_audit_gate.py",
)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_historical_digest_is_pinned_and_not_cli_configurable():
    assert module.RETAINED_SOURCE_SHA256 == "f426b3ee9fcf8833e5e0630c39761f7ac2e4d444fd93905880cd2f4ab3dc6a91"
    with pytest.raises(ValueError, match="retained_source_digest_mismatch"):
        module.verify_retained_source(b'{"synthetic":true}')


@pytest.mark.parametrize("change", ["run_id", "environment", "evidence", "whitespace"])
def test_exact_bytes_required_even_when_logical_gate_remains_blocked(monkeypatch, change):
    # Unit fixture only; the real retained bytes are qualified separately and
    # contain private resource references which must not enter the repository.
    document = {"run_id": "original", "environment": {"ref": "fixture"},
                "evidence": {"sha256": "a" * 64}}
    raw = json.dumps(document).encode()
    expected = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(module, "RETAINED_SOURCE_SHA256", expected)
    assert module.verify_retained_source(raw) == expected
    if change == "whitespace":
        changed = raw + b"\n"
    else:
        document[change] = {"changed": "0" * 64}
        changed = json.dumps(document).encode()
    with pytest.raises(ValueError, match="retained_source_digest_mismatch"):
        module.verify_retained_source(changed)


@pytest.mark.parametrize("fault", [None, "timeout", "pass", "missing", "empty"])
@pytest.mark.parametrize("optimized", [False, True])
def test_target_qualification_survives_python_optimization(fault, optimized):
    import subprocess
    import sys

    rows = [{"verdict": "UNKNOWN", "unknown_reason": "NOT_RUN"} for _ in range(20)]
    if fault == "timeout":
        rows[0]["unknown_reason"] = "TIMEOUT"
    elif fault == "pass":
        rows[0].update(verdict="PASS", unknown_reason=None)
    elif fault == "missing":
        rows.pop()
    elif fault == "empty":
        rows = []
    code = (
        "import json,runpy,sys; "
        "runpy.run_path(sys.argv[1])['require_unexecuted_target'](json.loads(sys.argv[2]))"
    )
    result = subprocess.run(
        [sys.executable, *(["-O"] if optimized else []), "-c", code,
         str(SPEC.origin), json.dumps({"results": rows})], capture_output=True, timeout=10,
    )
    assert (result.returncode == 0) == (fault is None)
    if fault:
        assert b"target_run_not_fully_unexecuted" in result.stderr
