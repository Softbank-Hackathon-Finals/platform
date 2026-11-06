"""Authenticate and inspect both T14 network paths before a T19 PASS.

Understands collector formats 1 and 2. Format 2 remains unusable with the default
T5 catalog: parsing an artifact does not register a profile or authorize a plan.
"""

import hashlib
from dataclasses import asdict, dataclass
from datetime import timedelta

from .contract import GateError, decode, fingerprint, invoke, server_time, time
from .models import digest, identity
from .network import VerifiedNetworkProof


def output_digest(raw):
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def checked_digest(value):
    if not isinstance(value, str) or not value.startswith("sha256:"):
        raise GateError("NETWORK_DIGEST_REQUIRED")
    digest(value[7:])


@dataclass(frozen=True)
class NetworkPathRegistration:
    role: str
    port: int
    target_binding_sha256: str
    policy_sha256: str
    registration_sha256: str | None = None

    def __post_init__(self):
        if (
            self.role not in {"database", "pooler"}
            or type(self.port) is not int
            or not 1 <= self.port <= 65535
        ):
            raise GateError("NETWORK_PATH_REQUIRED")
        checked_digest(self.target_binding_sha256)
        checked_digest(self.policy_sha256)
        if self.registration_sha256 is not None:
            checked_digest(self.registration_sha256)


@dataclass(frozen=True)
class NetworkRunRegistration:
    context_digest: str
    side: str
    app: str
    environment_ref: str
    run_id: str
    test_id: str
    method_version: str
    paths: tuple[NetworkPathRegistration, ...]
    valid_until: str

    def __post_init__(self):
        digest(self.context_digest)
        for value in (self.app, self.environment_ref, self.run_id):
            identity(value)
        if (
            self.side not in {"source", "target"}
            or self.test_id not in {"NET-01.2", "NET-01.3"}
            or self.method_version not in {"1", "2"}
            or type(self.paths) is not tuple
            or len(self.paths) != 2
            or not all(isinstance(p, NetworkPathRegistration) for p in self.paths)
            or tuple(p.role for p in self.paths) != ("database", "pooler")
            or len({p.target_binding_sha256 for p in self.paths}) != 2
        ):
            raise GateError("BOTH_REGISTERED_NETWORK_PATHS_REQUIRED")
        if self.method_version == "1" and (
            tuple(p.port for p in self.paths) != (5432, 6432)
            or any(p.registration_sha256 is not None for p in self.paths)
        ):
            raise GateError("NETWORK_METHOD_MISMATCH")
        if self.method_version == "2" and any(p.registration_sha256 is None for p in self.paths):
            raise GateError("LISTENER_REGISTRATION_REQUIRED")
        time(self.valid_until)

    @property
    def fingerprint(self):
        return fingerprint(asdict(self))


def _require(condition):
    if not condition:
        raise GateError("NETWORK_BUNDLE_UNVERIFIED")


def _verify_collected_bundle(raw, children, *, registration, expected_digest, now):
    """Pure verifier. Host authenticates original bytes and immutable registration.

    Returns the last instant all observations remain within the 60-second window.
    Caller must also cap this by authority and registration expiries.
    """
    _require(isinstance(registration, NetworkRunRegistration))
    r = registration
    _require(type(raw) is bytes and 0 < len(raw) <= 16384 and output_digest(raw) == expected_digest)
    _require(now < time(r.valid_until))
    aggregate = decode(raw)
    base_keys = {
        "format_version",
        "run_id",
        "app",
        "environment",
        "test_id",
        "results",
        "observed",
        "unknown_reason",
        "collected_at",
    }
    extra = (
        {"required_ports"}
        if r.method_version == "1"
        else {"method_version", "required_roles", "registration_binding_sha256"}
    )
    _require(set(aggregate) == base_keys | extra)

    def envelope(doc):
        _require(
            doc["format_version"] == r.method_version
            and doc["run_id"] == r.run_id
            and doc["app"] == r.app
            and doc["environment"] == r.environment_ref
            and doc["test_id"] == r.test_id
            and doc["observed"] == "deny"
            and doc["unknown_reason"] is None
        )
        collected = time(doc["collected_at"])
        _require(0 <= (now - collected).total_seconds() <= 60)
        return collected

    aggregate_at = envelope(aggregate)
    if r.method_version == "1":
        _require(aggregate["required_ports"] == [5432, 6432])
    else:
        _require(
            aggregate["method_version"] == "2"
            and aggregate["required_roles"] == ["database", "pooler"]
        )
        bindings = [
            {
                "role": p.role,
                "target_binding_sha256": p.target_binding_sha256,
                "registration_sha256": p.registration_sha256,
            }
            for p in r.paths
        ]
        _require(aggregate["registration_binding_sha256"] == "sha256:" + fingerprint(bindings))
    rows = aggregate["results"]
    _require(
        type(rows) is list and len(rows) == 2 and type(children) is dict and len(children) == 2
    )
    seen, dates = set(), [aggregate_at]
    for path, row in zip(r.paths, rows, strict=True):
        row_keys = {
            "port",
            "target_binding_sha256",
            "output_sha256",
            "observed",
            "positive_control",
            "unknown_reason",
        }
        if r.method_version == "2":
            row_keys |= {"role", "registration_sha256"}
        _require(
            type(row) is dict
            and set(row) == row_keys
            and type(row["port"]) is int
            and row["port"] == path.port
            and row["target_binding_sha256"] == path.target_binding_sha256
            and row["observed"] == "deny"
            and row["positive_control"] == "pass"
            and row["unknown_reason"] is None
        )
        if r.method_version == "2":
            _require(
                row["role"] == path.role and row["registration_sha256"] == path.registration_sha256
            )
        key = row["output_sha256"]
        checked_digest(key)
        _require(key not in seen and key in children)
        seen.add(key)
        child_raw = children[key]
        _require(
            type(child_raw) is bytes
            and 0 < len(child_raw) <= 16384
            and output_digest(child_raw) == key
        )
        child = decode(child_raw)
        _require(
            set(child)
            == {
                "format_version",
                "run_id",
                "app",
                "environment",
                "test_id",
                "target_binding_sha256",
                "policy_sha256",
                "observations",
                "positive_control",
                "observed",
                "unknown_reason",
                "collected_at",
                "qualification",
            }
        )
        collected = envelope(child)
        dates.append(collected)
        _require(
            collected <= aggregate_at
            and child["target_binding_sha256"] == path.target_binding_sha256
            and child["policy_sha256"] == path.policy_sha256
            and child["positive_control"] == "pass"
            and child["qualification"] == "bounded-connectivity-with-controls-not-policy-causality"
        )
        needed = {"before_target", "before_agent", "probe", "after_target", "after_agent"}
        if r.test_id == "NET-01.3":
            needed |= {"before_dns", "after_dns"}
        observations = child["observations"]
        _require(type(observations) is dict and set(observations) == needed)
        probe_at = time(observations["probe"]["observed_at"])
        for label, item in observations.items():
            keys = {"outcome", "elapsed_ms", "observed_at"}
            if r.method_version == "2":
                keys |= {"coordinator_started_at", "agent_observed_at", "max_clock_offset_ms"}
            _require(
                type(item) is dict
                and set(item) == keys
                and type(item["elapsed_ms"]) is int
                and 0 <= item["elapsed_ms"] <= 5000
            )
            at = time(item["observed_at"])
            dates.append(at)
            _require(0 <= (now - at).total_seconds() <= 60 and at <= collected)
            if label == "probe":
                _require(item["outcome"] in {"refused", "timeout"})
                if item["outcome"] == "timeout":
                    _require(item["elapsed_ms"] >= 3000)
            else:
                _require(item["outcome"] == ("resolved" if label.endswith("dns") else "connected"))
                _require(at <= probe_at if label.startswith("before") else at >= probe_at)
            if r.method_version == "2":
                start = time(item["coordinator_started_at"])
                agent = time(item["agent_observed_at"])
                dates.append(start)
                _require(
                    type(item["max_clock_offset_ms"]) is int
                    and item["max_clock_offset_ms"] == 1000
                    and 0 <= (at - start).total_seconds() <= 5
                    and 0 <= (now - start).total_seconds() <= 60
                    and start - timedelta(seconds=1) <= agent <= at + timedelta(seconds=1)
                )
    _require(seen == set(children) and (max(dates) - min(dates)).total_seconds() <= 60)
    return min(dates) + timedelta(seconds=60)


def verify_collected_bundle(raw, children, *, registration, expected_digest, now):
    try:
        return _verify_collected_bundle(
            raw, children, registration=registration, expected_digest=expected_digest, now=now
        )
    except GateError:
        raise
    except (TypeError, KeyError, AttributeError, ValueError):
        raise GateError("NETWORK_BUNDLE_UNVERIFIED") from None


class CollectedNetworkProofAuthority:
    """Server-only bridge for authenticated T14 original aggregate + two children."""

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
            callable(x)
            for x in (
                binding_provider,
                authenticate_binding,
                artifact_provider,
                authenticate_artifact,
                clock,
            )
        ):
            raise GateError("EXPLICIT_SERVER_AUTHORITY_REQUIRED")
        self.binding_provider, self.authenticate_binding = binding_provider, authenticate_binding
        self.artifact_provider, self.authenticate_artifact = (
            artifact_provider,
            authenticate_artifact,
        )
        self.clock = lambda: server_time(clock)

    def __call__(self, actor, ctx, side, test_id, row):
        from .service import accept

        deadlines = []
        binding = invoke(self.binding_provider, actor, ctx, side, test_id)
        _require(
            isinstance(binding, NetworkRunRegistration)
            and binding.context_digest == ctx.fingerprint
            and binding.side == side
            and binding.test_id == test_id
            and binding.app == ctx.app
            and binding.environment_ref == getattr(ctx, side + "_environment_ref")
            and binding.method_version == row.get("method_version")
        )

        def authenticate_binding():
            accept(
                self.authenticate_binding,
                actor,
                ctx,
                binding,
                expected=(ctx.fingerprint, binding.fingerprint, "network-binding"),
                deadlines=deadlines,
                clock=self.clock,
            )

        authenticate_binding()
        retained = {}

        def artifact(key):
            checked_digest(key)
            raw = invoke(self.artifact_provider, actor, ctx, side, key)
            _require(type(raw) is bytes and 0 < len(raw) <= 16384 and output_digest(raw) == key)
            accept(
                self.authenticate_artifact,
                actor,
                ctx,
                side,
                key,
                raw,
                expected=(ctx.fingerprint, key[7:], "network-artifact"),
                deadlines=deadlines,
                clock=self.clock,
            )
            retained[key] = raw
            return raw

        key = row["evidence"]["output_sha256"]
        raw = artifact(key)
        aggregate = decode(raw)
        _require(time(aggregate.get("collected_at")) == time(row["evidence"].get("collected_at")))
        rows = aggregate.get("results")
        _require(type(rows) is list and len(rows) == 2)
        children = {}
        for item in rows:
            _require(type(item) is dict and isinstance(item.get("output_sha256"), str))
            child_key = item["output_sha256"]
            _require(child_key != key and child_key not in children)
            children[child_key] = artifact(child_key)
        until = verify_collected_bundle(
            raw, children, registration=binding, expected_digest=key, now=self.clock()
        )
        _require(invoke(self.binding_provider, actor, ctx, side, test_id) == binding)
        authenticate_binding()
        for saved_key, saved_raw in tuple(retained.items()):
            _require(artifact(saved_key) == saved_raw)
        until = min(until, time(binding.valid_until), *deadlines)
        _require(self.clock() < until)
        return VerifiedNetworkProof(
            ctx.fingerprint, side, test_id, key, until, ("database", "pooler"), binding.run_id
        )
