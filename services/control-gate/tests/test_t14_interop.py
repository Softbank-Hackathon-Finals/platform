"""Real T14 producer code with synthetic probes, not actual AWS/RKE2 evidence."""

from dataclasses import replace
from pathlib import Path

from bravo_gate.contract import canonical, fingerprint
from bravo_gate.models import RunSnapshot


def test_t14_producer_to_t19_block_fix_and_unknown(service, monkeypatch):
    sibling = Path(__file__).resolve().parents[2] / "control-runner/src"
    monkeypatch.syspath_prepend(str(sibling))
    from bravo_controls.runner import Catalog, Environment, Observation, Runner

    app, state = service
    catalog = Catalog(app.contract.catalog, app.contract.schema)
    original = state["context"]
    specs = catalog.profile(original.profile_id, original.profile_version)
    def collect(context, spec):
        return Observation(context, spec.test_id, spec.method_version, spec.expected,
                           "pass" if spec.positive_control == "required" else "not_applicable",
                           state["now"], "contract-test", "1", "sha256:" + "a" * 64)
    for mode, expected in [("open-bucket", "BLOCK"), ("fixed", "PASS"), ("missing", "BLOCK")]:
        for side in ("source", "target"):
            probes = {spec.test_id: collect for spec in specs}
            if side == "target" and mode == "open-bucket":
                probes["IAM-01.1"] = lambda context, spec: replace(collect(context, spec), observed="allow")
            if side == "target" and mode == "missing":
                probes.pop("AUD-01.1")
            run = Runner(catalog, probes, clock=lambda: state["now"]).run(
                profile_id=original.profile_id, profile_version=original.profile_version,
                app=original.app, environment=Environment(getattr(original, side + "_environment_id"),
                                                         side, getattr(original, side + "_environment_ref")))
            state["runs"][side] = RunSnapshot(getattr(original, side + "_ref"), canonical(run))
        state["context"] = replace(original, **{side + "_digest": state["runs"][side].fingerprint
                                               for side in ("source", "target")})
        result = app.evaluate("actor", "context")
        assert result["decision"] == expected
        assert fingerprint(result) == fingerprint(app.evaluate("actor", "context"))
