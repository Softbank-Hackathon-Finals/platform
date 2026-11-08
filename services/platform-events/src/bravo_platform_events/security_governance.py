"""Security governance conditions inside the platform's existing plan authority.

The security service evaluates evidence. It never approves or executes a change.
Registrations live in ``plan_commands`` and every read, replacement and revocation
uses the same plan transaction as T31/T10 authority.
"""

import hashlib
import json
import os
import re
import ssl
from dataclasses import replace
from pathlib import Path

from bravo_gate.contract import decode as decode_plan
from bravo_gate.contract import fingerprint
from bravo_gate.t10 import PlanGateSession, T10GateAuthority
from sova_security.adapters.modernization_codec import decode, encode
from sova_security.adapters.remote_review import RemoteReviewClient
from sova_security.adapters.review_https import HTTPSJSONTransport
from sova_security.adapters.review_mounts import MountedTokenCredential
from sova_security.contracts.commands import ContractError, utc
from sova_security.domain.review_service import HostReviewBinding, ReviewRegistration
from sova_security.domain.security_program import ProgramKind, ProgramPhase

from .change import CurrentChange
from .contract import validate_identifier
from .cutover import CurrentCutover, _get, _put
from .plan_api import authorized_plan
from .plans import _canonical
from .store import EventError

_SHA = re.compile(r"[0-9a-f]{40}")
_KINDS = frozenset(ProgramKind)
_PHASES = frozenset(ProgramPhase)


def _key(kind, phase):
    return f"security-program-{kind.value}-{phase.value}"


def _latest(connection, plan_id, kind, phase):
    prefix = _key(kind, phase) + "-"
    row = connection.execute("SELECT request_json,receipt_json FROM plan_commands "
                             "WHERE plan_id=? AND command_key LIKE ? ORDER BY command_key DESC LIMIT 1",
                             (plan_id, prefix + "%")).fetchone()
    return (json.loads(row["request_json"]), json.loads(row["receipt_json"])) if row else None


def _registration(raw):
    try:
        if type(raw) is str:
            raw = raw.encode()
        elif type(raw) is dict:
            raw = json.dumps(raw, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode()
        value = decode(raw)
    except Exception:
        raise EventError("security_registration_invalid") from None
    if type(value) is not ReviewRegistration or value.program.kind not in _KINDS or value.phase not in _PHASES:
        raise EventError("security_registration_invalid")
    return value


def _host_binding(program, session, plan, baseline_sha):
    """Project trusted platform values without pretending another source revision.

    This is the platform-owned half of the cross-repository contract. The security
    package's adapter deliberately accepts only revisions qualified in that repo;
    the platform image instead binds its own immutable ``sourceRevision`` here.
    """
    if type(session) is not PlanGateSession or _SHA.fullmatch(baseline_sha) is None:
        raise ContractError("Pinned platform host and source revision required")
    spec = decode_plan(plan["spec_json"].encode())
    context = session.context
    prefix = "recheck_" if session.gate_phase == "post_apply" else ""
    if (session.plan_id != plan["plan_id"] or spec["plan_id"] != plan["plan_id"]
            or context.tenant_id != plan["tenant_id"] or spec["tenant_id"] != context.tenant_id
            or context.tenant_id != program.target.tenant_id
            or context.app != program.target.application_id or spec["workload_id"] != context.app
            or spec["plan_digest"] != program.subject_digest
            or context.plan_digest != program.subject_digest
            or spec.get(prefix + "profile_id") != context.profile_id
            or spec.get(prefix + "profile_version") != context.profile_version
            or spec["source_environment_id"] != program.source.environment_id
            or spec["target_environment_id"] != program.target.environment_id
            or context.source_environment_id != program.source.environment_id
            or context.target_environment_id != program.target.environment_id):
        raise ContractError("Platform plan, profile, environment or tenant differs")
    return HostReviewBinding("platform-t10", baseline_sha, plan["plan_id"], program.subject_digest,
                             session.fingerprint, context.profile_id, context.profile_version,
                             session.gate_phase, session.application_id)


class SecurityProgramRecords:
    """Current program registrations in the existing plan command journal."""

    def __init__(self, store, *, authorize, load_session, baseline_sha, clock):
        if (not all(map(callable, (authorize, load_session, clock)))
                or _SHA.fullmatch(baseline_sha or "") is None):
            raise ValueError("security_governance_ports_required")
        self.store, self.authorize, self.load_session = store, authorize, load_session
        self.baseline_sha, self.clock = baseline_sha, clock

    def _plan(self, connection, principal, plan_id, permission):
        plan = authorized_plan(connection, principal, plan_id, None)
        if self.authorize(connection, principal, plan, permission) is not True:
            raise EventError("forbidden")
        return plan

    def register(self, principal, plan_id, document, *, expected_revision, command_key):
        """Trusted-host enrollment; decoded bytes are not authority by themselves."""
        validate_identifier(command_key)
        registration = _registration(document)
        if type(expected_revision) is not int or not 0 <= expected_revision < 10**12:
            raise ValueError("invalid_security_registration_revision")
        request = {"registration": json.loads(encode(registration)), "expected_revision": expected_revision}
        command = "security-program-command-" + hashlib.sha256(command_key.encode()).hexdigest()
        with self.store._transaction(write=True, plan_id=plan_id) as connection:
            plan = self._plan(connection, principal, plan_id, "security:register")
            old = _get(connection, plan_id, command)
            if old:
                if _canonical(old[0]) != _canonical(request):
                    raise EventError("command_key_conflict")
                return old[1]
            registration.require_current(self.clock())
            session = self.load_session(connection, principal, plan)
            if registration.host_binding != _host_binding(
                    registration.program, session, plan, self.baseline_sha):
                raise EventError("security_registration_binding_mismatch")
            current = _latest(connection, plan_id, registration.program.kind, registration.phase)
            revision = current[1]["revision"] if current else 0
            if revision != expected_revision:
                raise EventError("version_conflict")
            receipt = {"registration": request["registration"], "fingerprint": registration.fingerprint,
                       "revision": revision + 1, "recorded_at": utc(self.clock()).isoformat()}
            key = _key(registration.program.kind, registration.phase) + f"-{receipt['revision']:013}"
            _put(connection, plan_id, key, request, receipt)
            _put(connection, plan_id, command, request, receipt)
            return receipt

    def revoke(self, principal, plan_id, *, kind, phase, expected_revision, command_key):
        validate_identifier(command_key)
        kind, phase = ProgramKind(kind), ProgramPhase(phase)
        if type(expected_revision) is not int or expected_revision < 1:
            raise ValueError("invalid_security_registration_revision")
        request = {"kind": kind.value, "phase": phase.value, "expected_revision": expected_revision}
        command = "security-program-command-" + hashlib.sha256(command_key.encode()).hexdigest()
        with self.store._transaction(write=True, plan_id=plan_id) as connection:
            self._plan(connection, principal, plan_id, "security:register")
            old = _get(connection, plan_id, command)
            if old:
                if _canonical(old[0]) != _canonical(request):
                    raise EventError("command_key_conflict")
                return old[1]
            current = _latest(connection, plan_id, kind, phase)
            if current is None or current[1]["revision"] != expected_revision:
                raise EventError("version_conflict")
            receipt = {"registration": None, "fingerprint": None, "revision": expected_revision + 1,
                       "recorded_at": utc(self.clock()).isoformat()}
            key = _key(kind, phase) + f"-{receipt['revision']:013}"
            _put(connection, plan_id, key, request, receipt)
            _put(connection, plan_id, command, request, receipt)
            return receipt

    def load(self, connection, principal, plan, kind, phase):
        kind, phase = ProgramKind(kind), ProgramPhase(phase)
        current = self._plan(connection, principal, plan["plan_id"], "security:read")
        if current != dict(plan):
            raise EventError("security_plan_changed")
        saved = _latest(connection, plan["plan_id"], kind, phase)
        if saved is None or saved[1].get("registration") is None:
            raise EventError("security_registration_unavailable")
        registration = _registration(saved[1]["registration"])
        registration.require_current(self.clock())
        if (registration.program.kind != kind or registration.phase != phase
                or registration.fingerprint != saved[1]["fingerprint"]):
            raise EventError("security_registration_changed")
        session = self.load_session(connection, principal, plan)
        if registration.host_binding != _host_binding(
                registration.program, session, plan, self.baseline_sha):
            raise EventError("security_registration_binding_mismatch")
        return registration


class SecurityReviewAuthority:
    """Fresh remote evidence conditions around an existing platform authority."""

    def __init__(self, *, upstream, records, requirements, transport, credential_for, subject_id, clock):
        if (type(upstream) not in {T10GateAuthority} and not callable(upstream)) or not isinstance(records, SecurityProgramRecords):
            raise ValueError("security_governance_authority_required")
        requirements = tuple((ProgramKind(kind), ProgramPhase(phase)) for kind, phase in requirements)
        if not requirements or len(set(requirements)) != len(requirements):
            raise ValueError("security_governance_requirements_invalid")
        validate_identifier(subject_id)
        self.upstream, self.records, self.requirements = upstream, records, requirements
        self.transport, self.credential_for = transport, credential_for
        self.subject_id, self.clock = subject_id, clock

    def __call__(self, connection, principal, plan, **kwargs):
        started = utc(self.clock())
        first = self.upstream(connection, principal, plan, **kwargs)
        session, registrations, responses = review_conditions(connection, principal, plan,
            records=self.records, requirements=self.requirements, transport=self.transport,
            credential_for=self.credential_for, subject_id=self.subject_id, clock=self.clock)
        latest = self.upstream(connection, principal, plan, **kwargs)
        current_session = self.records.load_session(connection, principal, plan)
        finished = utc(self.clock())
        current_registrations = tuple(self.records.load(connection, principal, plan, kind, phase)
                                      for kind, phase in self.requirements)
        if (latest != first or current_session.fingerprint != session.fingerprint
                or current_registrations != registrations or finished < started
                or any(finished >= response.valid_until for response in responses)):
            raise ContractError("Platform or remote security authority changed during admission")
        revision = fingerprint({"kind": "security-governance.v1", "upstream": latest.revision,
                                "session": session.fingerprint,
                                "registrations": [value.fingerprint for value in registrations]})
        if type(latest) not in {CurrentChange, CurrentCutover}:
            raise ContractError("Known platform authority result required")
        return replace(latest, revision=revision)


def review_conditions(connection, principal, plan, *, records, requirements, transport,
                      credential_for, subject_id, clock):
    """Run fresh enforce reviews without creating an approval or execution claim."""
    requirements = tuple((ProgramKind(kind), ProgramPhase(phase)) for kind, phase in requirements)
    if (not isinstance(records, SecurityProgramRecords) or not requirements
            or len(set(requirements)) != len(requirements)):
        raise ValueError("security_governance_requirements_invalid")
    validate_identifier(subject_id)
    started = utc(clock())
    session = records.load_session(connection, principal, plan)
    registrations = tuple(records.load(connection, principal, plan, kind, phase)
                          for kind, phase in requirements)

    def current(scope, subject_digest, phase):
        matches = [registration for registration in registrations
                   if registration.scope == scope
                   and registration.program.subject_digest == subject_digest
                   and registration.phase == phase]
        if len(matches) != 1:
            raise ContractError("Exact locked platform registration required")
        current_registration = records.load(
            connection, principal, plan, matches[0].program.kind, phase)
        if current_registration != matches[0]:
            raise ContractError("Locked platform registration changed")
        return current_registration

    client = RemoteReviewClient(transport=transport, credential_for=credential_for,
                                current_registration=current, subject_id=subject_id, clock=clock)
    responses = []
    for registration in registrations:
        response = client.review(scope=registration.scope,
                                 subject_digest=registration.program.subject_digest,
                                 phase=registration.phase, mode="enforce",
                                 not_before=registration.not_before)
        if not response.review.passed:
            raise ContractError("Additional security program has not passed")
        responses.append(response)
    finished = utc(clock())
    current_session = records.load_session(connection, principal, plan)
    current_registrations = tuple(records.load(connection, principal, plan, kind, phase)
                                  for kind, phase in requirements)
    if (finished < started or current_session.fingerprint != session.fingerprint
            or current_registrations != registrations
            or any(finished >= response.valid_until for response in responses)):
        raise ContractError("Platform or remote security authority changed during review")
    return session, registrations, tuple(responses)


def review_ports(config, *, clock):
    """Build fixed-origin TLS and a rotating projected workload credential."""
    required = {"url", "ca_file", "token_file", "subject_id", "source_revision", "registrar"}
    deployed_revision = os.environ.get("BRAVO_PLATFORM_SOURCE_REVISION")
    if type(config) is dict and config.get("source_revision") == "deployed":
        # T49: follow the running image's pinned revision, so an image pin no longer needs a config change.
        config = config | {"source_revision": deployed_revision or ""}
    if (type(config) is not dict or set(config) != required
            or _SHA.fullmatch(config.get("source_revision", "")) is None
            or deployed_revision != config.get("source_revision")):
        raise ValueError("security_review_configuration_invalid")
    registrar = config["registrar"]
    if (type(registrar) is not dict or set(registrar) != {"tenant_id", "subject_id"}
            or not all(type(value) is str and value for value in registrar.values())):
        raise ValueError("security_review_configuration_invalid")
    ca_file, token_file = Path(config["ca_file"]), Path(config["token_file"])
    if not ca_file.is_absolute() or not ca_file.is_file() or not token_file.is_absolute() or not token_file.is_file():
        raise ValueError("security_review_mount_required")
    context = ssl.create_default_context(cafile=ca_file)
    return (HTTPSJSONTransport(config["url"], tls_context=context),
            MountedTokenCredential(token_file, clock=clock), config["subject_id"], config["source_revision"])
