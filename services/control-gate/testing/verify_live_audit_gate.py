"""Owned local T34 canaries → T14 full runs → T19 signed input verification.

Real k3s/CNPG and Collector, ephemeral local signing keys/authority/store.
No team/cloud credentials. No operational approval or complete security PASS.
"""

import argparse
import hashlib
import json
import os
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bravo_gate.contract import GateError, T5Contract, canonical, fingerprint
from bravo_gate.models import GateContext, RunSnapshot
from bravo_gate.service import GateService
from bravo_gate.signatures import DOMAIN, AuthorityEnvelope, AuthorityKey, Ed25519Authority

# Independently retained #216 source bytes. This qualification is tied to that
# historical run, not a generic upload entry point or caller-supplied hash.
RETAINED_SOURCE_SHA256 = "f426b3ee9fcf8833e5e0630c39761f7ac2e4d444fd93905880cd2f4ab3dc6a91"


def verify_retained_source(raw: bytes) -> str:
    digest = hashlib.sha256(raw).hexdigest()
    if digest != RETAINED_SOURCE_SHA256:
        raise ValueError("retained_source_digest_mismatch")
    return digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner-checkout", type=Path)
    parser.add_argument("--source-run", type=Path)
    parser.add_argument("--target-run", type=Path)
    parser.add_argument("--mode", choices=("audit-pair", "restore-partial", "source-scan-partial"), default="audit-pair")
    parser.add_argument("--private-output", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.private_output.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    if args.mode == "audit-pair":
        if args.runner_checkout is None or args.source_run or args.target_run:
            parser.error("audit-pair requires only runner-checkout")
        runner = args.runner_checkout.resolve() / "services/control-runner"
        sys.path[:0] = [str(runner / "src"), str(runner / "tests/integration")]
        # Explicit operator-selected checkout, never a user upload or URL.
        from verify_live_audit_canaries import run
    elif not args.source_run or not args.target_run or args.runner_checkout:
        parser.error("partial qualification requires only existing source-run and target-run")

    result = {
        "status": "INCOMPLETE",
        "scope": "two owned local k3s/CNPG environments; ephemeral test authority",
        "cloud_resources_changed": False,
    }
    try:
        if args.mode == "audit-pair":
            checks = {side: run(root / side, side=side) for side in ("source", "target")}
            assert all(
                check["status"] == "PASS" and check["owned_resources_removed"]
                for check in checks.values()
            )
            raw = {side: (root / side / "control-run.json").read_bytes() for side in checks}
        else:
            # Explicit local test inputs, not an operational upload/trust API.
            raw = {"source": args.source_run.read_bytes(), "target": args.target_run.read_bytes()}
            if args.mode == "source-scan-partial":
                result["source_run_sha256"] = verify_retained_source(raw["source"])
            source = json.loads(raw["source"])
            target = json.loads(raw["target"])
            assert len(source["results"]) == len(target["results"]) == 20
            source_test = "SEC-01.2" if args.mode == "source-scan-partial" else "DR-01.1"
            assert {r["test_id"] for r in source["results"] if r["verdict"] == "PASS"} == {source_test}
            assert all(r["verdict"] == "UNKNOWN" for r in target["results"])
            if args.mode == "source-scan-partial":
                assert all(r["unknown_reason"] == "NOT_RUN" for r in target["results"])
                result["scope"] = "retained AWS source SEC-01.2 run and an explicitly unexecuted target; local test authority"
            else:
                result["scope"] = "one actual local restore run and an explicitly unverified target; test authority"
        snapshots = {side: RunSnapshot(side + "-registered", data) for side, data in raw.items()}
        contract = T5Contract()
        ctx = GateContext(
            "local-trial-tenant",
            "demo-chat",
            {"audit-pair": "local-audit-gate", "restore-partial": "local-restore-partial-gate",
             "source-scan-partial": "local-source-scan-partial-gate"}[args.mode],
            fingerprint({"local_plan": str(root.name)}),
            "full",
            "1",
            contract.catalog_digest,
            *[
                snapshots[side].document["environment"][field]
                for side in ("source", "target")
                for field in ("id", "ref")
            ],
            snapshots["source"].ref,
            snapshots["target"].ref,
            snapshots["source"].fingerprint,
            snapshots["target"].fingerprint,
        )
        now = datetime.now(UTC)
        offset = timedelta(0)
        keys, envelopes, reports = {}, {}, {}

        def sign(purpose, subject):
            private = Ed25519PrivateKey.generate()
            keys[purpose] = AuthorityKey(
                private.public_key().public_bytes_raw().hex(),
                (ctx.tenant_id,),
                (purpose,),
                (now - timedelta(seconds=1)).isoformat(),
                (now + timedelta(minutes=2)).isoformat(),
                False,
            )
            unsigned = AuthorityEnvelope(
                purpose,
                "local-test-key",
                purpose,
                ctx.fingerprint,
                subject,
                now.isoformat(),
                (now + timedelta(minutes=1)).isoformat(),
                "0" * 128,
            )
            envelopes[purpose] = replace(
                unsigned, signature_hex=private.sign(DOMAIN + canonical(unsigned.payload)).hex()
            )
            # Private signing keys are not persisted or supplied to the gate.

        for purpose, subject in (
            ("context", ctx.fingerprint),
            ("run-source", ctx.source_digest),
            ("run-target", ctx.target_digest),
        ):
            sign(purpose, subject)
        authority = Ed25519Authority(
            envelope_provider=lambda _, __, p: envelopes[p],
            key_provider=lambda issuer, _: keys[issuer],
            clock=lambda: datetime.now(UTC) + offset,
        )
        service = GateService(
            contract=contract,
            context_provider=lambda *_: ctx,
            run_provider=lambda _, __, side: snapshots[side],
            approval_provider=lambda *_: None,
            authenticate_context=lambda _, c: authority.verify(c, c.fingerprint, "context"),
            authenticate_run=lambda _, c, side, s: authority.verify(
                c, s.fingerprint, "run-" + side
            ),
            authenticate_approval=lambda *_: False,
            report_provider=lambda _, c: reports.get(c.gate_id),
            authenticate_report=lambda _, c, s: authority.verify(c, s.fingerprint, "gate-result"),
            authorize=lambda actor, _, action: (
                actor == "local-test-operator" and action in {"gate:evaluate", "gate:consume"}
            ),
            clock=lambda: datetime.now(UTC) + offset,
        )
        report = service.evaluate("local-test-operator", "local-context")
        reports[ctx.gate_id] = RunSnapshot(ctx.gate_id, canonical(report))
        sign("gate-result", reports[ctx.gate_id].fingerprint)
        rows = {row["test_id"]: row for row in report["rows"]}
        assert len(rows) == 20
        if args.mode == "audit-pair":
            audit_ids = {"AUD-01.1", "AUD-01.2", "AUD-01.3"}
            assert {t for t, row in rows.items() if row["decision"] == "PASS"} == audit_ids
            assert all(row["reason"] == "SOURCE_UNKNOWN" for t, row in rows.items() if t not in audit_ids)
        else:
            assert all(row["decision"] == "BLOCK" for row in rows.values())
            assert rows[source_test]["reason"] == "TARGET_UNKNOWN"
            assert all(row["reason"] == "SOURCE_UNKNOWN" for t, row in rows.items() if t != source_test)
        assert report["decision"] == "BLOCK"

        def rejected(callback):
            try:
                callback()
            except GateError as error:
                return str(error)
            raise AssertionError("gate_accepted_invalid_input")

        legitimate_block = rejected(
            lambda: service.require_security(
                "local-test-operator", "local-context", canonical(report)
            )
        )
        forged = json.loads(canonical(report))
        forged["decision"] = "PASS"
        forged_block = rejected(
            lambda: service.require_security(
                "local-test-operator", "local-context", canonical(forged)
            )
        )
        offset = timedelta(minutes=3)
        expired_block = rejected(lambda: service.evaluate("local-test-operator", "local-context"))
        offset = timedelta(0)
        old_key = keys["run-target"]
        keys["run-target"] = replace(old_key, revoked=True)
        revoked_block = rejected(lambda: service.evaluate("local-test-operator", "local-context"))
        (root / "gate-comparison.json").write_bytes(canonical(report))
        result.update(
            status="PASS",
            full_profile_tests=20,
            actual_audit_controls_passed=3 if args.mode == "audit-pair" else 0,
            actual_source_restore_verified=args.mode == "restore-partial",
            retained_source_scan_loaded=args.mode == "source-scan-partial",
            unexecuted_controls_blocked=17 if args.mode == "audit-pair" else 20,
            whole_gate_decision=report["decision"],
            missing_controls_refuse_prerequisite=legitimate_block,
            fabricated_pass_rejected=forged_block,
            expired_authority_rejected=expired_block,
            revoked_authority_rejected=revoked_block,
            both_owned_environments_removed=True if args.mode == "audit-pair" else None,
            production_authority=False,
            application_cutover_executed=False,
        )
    finally:
        (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
