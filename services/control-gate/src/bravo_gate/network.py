"""Structured negative network probe proof; never interpret an excerpt as proof."""

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from .contract import GateError, decode, fingerprint, invoke, server_time, time


def verify_network_proof(raw, *, output_digest, binding, test_id, now):
    if type(raw) is not bytes or not 0 < len(raw) <= 16384:
        return False
    proof = decode(raw)
    if output_digest != "sha256:" + hashlib.sha256(raw).hexdigest():
        return False
    if set(proof) != {
        "kind",
        "test_id",
        "environment_ref",
        "target_ref",
        "probe_ref",
        "probe",
        "controls",
        "configuration_digest",
    }:
        return False
    if (
        proof["kind"] != "network-denial-proof.v1"
        or proof["test_id"] != test_id
        or proof["environment_ref"] != binding.environment_ref
        or test_id not in {"NET-01.2", "NET-01.3"}
    ):
        return False
    from .models import digest, identity

    try:
        digest(proof["configuration_digest"])
        if (
            proof["configuration_digest"] != binding.configuration_digest
            or proof["target_ref"] != binding.target_ref
            or proof["probe_ref"] != binding.probe_ref
            or now >= time(binding.valid_until)
        ):
            return False
        identity(proof["target_ref"])
        identity(proof["probe_ref"])
        probe = proof["probe"]
        if set(probe) != {"result", "at", "duration_ms", "target_ref", "probe_ref"}:
            return False
        if (
            probe["result"] not in {"refused", "timeout"}
            or type(probe["duration_ms"]) is not int
            or not 0 <= probe["duration_ms"] <= binding.timeout_ms
            or probe["target_ref"] != proof["target_ref"]
            or probe["probe_ref"] != proof["probe_ref"]
        ):
            return False
        if probe["result"] == "timeout" and probe["duration_ms"] != binding.timeout_ms:
            return False
        controls = proof["controls"]
        needed = (
            {"target", "probe-path"}
            if test_id == "NET-01.2"
            else {"target", "probe-dns", "probe-path"}
        )
        if type(controls) is not list or len(controls) != len(needed):
            return False
        if {c.get("kind") for c in controls} != needed:
            return False
        at = time(probe["at"])
        if not 0 <= (now - at).total_seconds() <= 60:
            return False
        for c in controls:
            if (
                set(c) != {"kind", "result", "at", "target_ref", "probe_ref"}
                or c["result"] != "success"
            ):
                return False
            if abs((time(c["at"]) - at).total_seconds()) > 60 or time(c["at"]) > now:
                return False
            if c["kind"] == "target" and (
                c["target_ref"] != proof["target_ref"]
                or c["probe_ref"] != binding.allowed_probe_ref
            ):
                return False
            if c["kind"] == "probe-path" and c["target_ref"] != binding.allowed_service_ref:
                return False
            if c["kind"] == "probe-dns" and c["target_ref"] != binding.dns_ref:
                return False
            if c["kind"] != "target" and c["probe_ref"] != proof["probe_ref"]:
                return False
        return True
    except (GateError, KeyError, TypeError, AttributeError):
        return False


@dataclass(frozen=True)
class NetworkBinding:
    context_digest: str
    environment_ref: str
    target_ref: str
    probe_ref: str
    allowed_probe_ref: str
    allowed_service_ref: str
    dns_ref: str
    configuration_digest: str
    timeout_ms: int
    valid_until: str

    def __post_init__(self):
        from .models import digest, identity

        digest(self.context_digest)
        digest(self.configuration_digest)
        for value in (
            self.environment_ref,
            self.target_ref,
            self.probe_ref,
            self.allowed_probe_ref,
            self.allowed_service_ref,
            self.dns_ref,
        ):
            identity(value)
        if type(self.timeout_ms) is not int or not 1000 <= self.timeout_ms <= 60000:
            raise GateError("BOUNDED_NETWORK_TIMEOUT_REQUIRED")
        time(self.valid_until)

    @property
    def fingerprint(self):
        return fingerprint(asdict(self))


@dataclass(frozen=True)
class VerifiedNetworkProof:
    context_digest: str
    side: str
    test_id: str
    output_digest: str
    valid_until: datetime


class NetworkProofAuthority:
    def __init__(
        self,
        *,
        binding_provider,
        authenticate_binding,
        artifact_provider,
        authenticate_artifact,
        clock,
    ):
        if not all(
            callable(v)
            for v in (
                binding_provider,
                authenticate_binding,
                artifact_provider,
                authenticate_artifact,
                clock,
            )
        ):
            raise GateError("EXPLICIT_SERVER_AUTHORITY_REQUIRED")
        self.binding_provider = binding_provider
        self.authenticate_binding = authenticate_binding
        self.artifact_provider = artifact_provider
        self.authenticate_artifact = authenticate_artifact
        self.clock = lambda: server_time(clock)

    def __call__(self, actor, ctx, side, test_id, row):
        from .service import accept

        deadlines = []
        binding = invoke(self.binding_provider, actor, ctx, side, test_id)
        if not isinstance(binding, NetworkBinding) or binding.context_digest != ctx.fingerprint:
            raise GateError("NETWORK_BINDING_REQUIRED")
        accept(
            self.authenticate_binding,
            actor,
            ctx,
            binding,
            expected=(ctx.fingerprint, binding.fingerprint, "network-binding"),
            deadlines=deadlines,
            clock=self.clock,
        )
        digest = row["evidence"]["output_sha256"]
        raw = invoke(self.artifact_provider, actor, ctx, side, digest)
        accept(
            self.authenticate_artifact,
            actor,
            ctx,
            side,
            digest,
            raw,
            expected=(ctx.fingerprint, digest.removeprefix("sha256:"), "network-artifact"),
            deadlines=deadlines,
            clock=self.clock,
        )
        now = self.clock()
        if not verify_network_proof(
            raw,
            output_digest=digest,
            binding=binding,
            test_id=test_id,
            now=now,
        ):
            raise GateError("NETWORK_DENIAL_UNVERIFIED")
        if (
            binding.environment_ref != getattr(ctx, side + "_environment_ref")
            or invoke(self.binding_provider, actor, ctx, side, test_id) != binding
        ):
            raise GateError("NETWORK_BINDING_CHANGED")
        accept(
            self.authenticate_binding,
            actor,
            ctx,
            binding,
            expected=(ctx.fingerprint, binding.fingerprint, "network-binding"),
            deadlines=deadlines,
            clock=self.clock,
        )
        if invoke(self.artifact_provider, actor, ctx, side, digest) != raw:
            raise GateError("NETWORK_EVIDENCE_CHANGED")
        accept(
            self.authenticate_artifact,
            actor,
            ctx,
            side,
            digest,
            raw,
            expected=(ctx.fingerprint, digest.removeprefix("sha256:"), "network-artifact"),
            deadlines=deadlines,
            clock=self.clock,
        )
        proof = decode(raw)
        until = min(
            time(proof["probe"]["at"]), *(time(c["at"]) for c in proof["controls"])
        ) + timedelta(seconds=60)
        until = min(until, time(binding.valid_until), *deadlines)
        if self.clock() >= until:
            raise GateError("NETWORK_EVIDENCE_EXPIRED")
        return VerifiedNetworkProof(ctx.fingerprint, side, test_id, digest, until)
