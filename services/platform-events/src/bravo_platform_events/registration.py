"""Create immutable registrations. A plan, its first event and its receipt commit together."""

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime

from .contract import validate_identifier
from .operator_identity import OperatorPrincipal
from .plans import PlanSpec, _canonical
from .registration_probe import KubernetesProbe
from .store import EventError


def digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


class Registry:
    def __init__(self, store, *, environments, releases, can_register: Callable,
                 probe=None, clock=None):
        self.store = store
        # Keys are (tenant, opaque connection ref); values are provisioned by the owner.
        self.environments = dict(environments)
        seen = set()
        for (tenant, ref), env in self.environments.items():
            for value in (tenant, ref, env.environment_id):
                validate_identifier(value)
            if tenant != env.tenant_id or (tenant, env.environment_id) in seen:
                raise ValueError("ambiguous_environment_reference")
            seen.add((tenant, env.environment_id))
        self.releases = {}
        for release in releases:
            key = (release.tenant_id, release.environment_id, release.repo_url, release.app_path, release.iac_path)
            if key in self.releases:
                raise ValueError("ambiguous_workload_release")
            self.releases[key] = release
        self.can_register = can_register
        self.probe = probe or KubernetesProbe()
        self.clock = clock or (lambda: datetime.now(UTC))

    def authorize(self, actor):
        if (not isinstance(actor, OperatorPrincipal) or not actor.issued_at <= self.clock().timestamp() < actor.expires_at
                or self.can_register(actor) is not True):
            raise EventError("forbidden")
        validate_identifier(actor.subject_id)
        validate_identifier(actor.tenant_id)

    def _env(self, tenant, ref):
        env = self.environments.get((tenant, ref))
        if env is None:
            raise EventError("registration_reference_unavailable")
        return env

    @staticmethod
    def _read(c, tenant, kind, object_id):
        row = c.execute("SELECT receipt_json FROM registrations WHERE tenant_id=? AND kind=? AND object_id=?",
                        (tenant, kind, object_id)).fetchone()
        if row is None:
            raise EventError("registration_reference_unavailable")
        return json.loads(row["receipt_json"])

    def _checked_environment(self, tenant, registered, release=None):
        env = self._env(tenant, registered["connection_ref"])
        if env.environment_id != registered["environment_id"] or env.role != registered["role"]:
            raise EventError("environment_changed")
        obs = self.probe.inspect(env, release)
        if any(obs[k] != registered["checks"][k] for k in ("namespace", "database_identity", "collector")):
            raise EventError("environment_changed")
        return obs

    def _workload_scope(self, tenant, source, app):
        if source["role"] != "source":
            raise EventError("environment_role_mismatch")
        if app["namespace"] != source["checks"]["namespace"]:
            raise EventError("namespace_mismatch")
        release = self.releases.get((tenant, source["environment_id"], app["repo_url"], app["app_path"], app["iac_path"]))
        if release is None:
            raise EventError("registration_reference_unavailable")
        obs = self._checked_environment(tenant, source, release)
        if obs.get("repo_deploy_match") is not True:
            raise EventError("workload_changed")
        scope = {k: obs[k] for k in ("namespace", "database", "database_identity", "source_sha",
                                      "image", "service_account", "resources", "rollout_identity")}
        scope.update(source_environment_id=source["environment_id"], repo_url=release.repo_url,
                     app_path=release.app_path, iac_path=release.iac_path, pod_label=release.label,
                     container=release.container, scope_version=1)
        return scope, obs["warnings"]

    def _prepare(self, tenant, kind, object_id, body):
        if kind == "environment":
            env = self._env(tenant, body["connection_ref"])
            if body["kind"] != env.kind or body["roles"] != [env.role]:
                raise EventError("environment_role_mismatch")
            return env.environment_id, dict(**body, environment_id=env.environment_id, role=env.role,
                                            checks=self.probe.inspect(env)), None
        if kind == "workload":
            with self.store._transaction() as c:
                source = self._read(c, tenant, "environment", body["source_environment_id"])
            scope, warnings = self._workload_scope(tenant, source, body)
            return object_id, dict(**body, workload_id=object_id, scope=scope,
                                   repo_deploy_match=True, warnings=warnings), None
        if kind != "plan":
            raise ValueError("invalid_registration_kind")
        with self.store._transaction() as c:
            app = self._read(c, tenant, "workload", body["workload_id"])
            source = self._read(c, tenant, "environment", app["source_environment_id"])
            target = self._read(c, tenant, "environment", body["target_environment_id"])
        if target["role"] != "target" or target["checks"]["database_identity"] == source["checks"]["database_identity"]:
            raise EventError("environment_role_mismatch")
        scope, warnings = self._workload_scope(tenant, source, app)
        if app["scope"] != scope:
            raise EventError("workload_changed")
        target_obs = self._checked_environment(tenant, target)
        scope_hash = digest(scope)
        plan_hash = digest(dict(plan_id=object_id, tenant_id=tenant, workload_id=app["workload_id"],
                                collection_scope_digest=scope_hash, target_environment_id=target["environment_id"],
                                target_database_identity=target_obs["database_identity"]))
        spec = PlanSpec(object_id, tenant, app["workload_id"], source["environment_id"],
                        target["environment_id"], plan_hash, scope_hash)
        receipt = dict(plan_id=object_id, workload_id=spec.workload_id,
                       source_environment_id=spec.source_environment_id, target_environment_id=spec.target_environment_id,
                       plan_digest=plan_hash, collection_scope_digest=scope_hash, state="COLLECT", version=1,
                       warnings=sorted(set(warnings + target_obs["warnings"])))
        return object_id, receipt, spec

    def register(self, actor, kind, body, key):
        self.authorize(actor)
        validate_identifier(key)
        tenant = actor.tenant_id
        request = _canonical({"actor": actor.subject_id, "body": body})

        def replay(c):
            row = c.execute("SELECT request_json,receipt_json FROM registrations "
                            "WHERE tenant_id=? AND kind=? AND command_key=?", (tenant, kind, key)).fetchone()
            if row is None:
                return None
            if row["request_json"] != request:
                raise EventError("command_key_conflict")
            return json.loads(row["receipt_json"])

        with self.store._transaction() as c:
            previous = replay(c)
            if previous:
                self.authorize(actor)
                return previous, False

        # Probe before taking the write lock. The referenced registrations are
        # immutable. A receipt records this observation, not a lease on Kubernetes.
        candidate_id = kind + "-" + digest([tenant, kind, key])[:32]
        object_id, receipt, spec = self._prepare(tenant, kind, candidate_id, body)
        # Same environment under two keys must serialize. Plans share the normal
        # plan lock, including concurrent calls from the trusted internal port.
        lock = object_id if kind == "plan" else "registry-" + digest([tenant, kind])
        with self.store._transaction(write=True, plan_id=lock) as c:
            self.authorize(actor)
            previous = replay(c)
            if previous:
                return previous, False
            exists = c.execute("SELECT 1 FROM registrations WHERE tenant_id=? AND kind=? AND object_id=?",
                               (tenant, kind, object_id)).fetchone()
            if exists:
                raise EventError("registration_exists")
            now = self.clock().isoformat()
            receipt.update(registered_at=now)
            if kind == "plan":
                # This receipt describes initial registration. An existing plan
                # may already have advanced, even if its immutable spec matches.
                if c.execute("SELECT 1 FROM plans WHERE plan_id=?", (object_id,)).fetchone():
                    raise EventError("plan_conflict")
                self.store._register_in_transaction(c, spec, run_id=object_id, occurred_at=now)
            c.execute("INSERT INTO registrations(tenant_id,kind,object_id,command_key,request_json,receipt_json) "
                      "VALUES (?,?,?,?,?,?)", (tenant, kind, object_id, key, request, _canonical(receipt)))
        return receipt, True
