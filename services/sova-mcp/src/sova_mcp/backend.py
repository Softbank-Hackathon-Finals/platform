"""SOVA reads and registrations for one verified actor, on the same T10 store and Registry.

No new state machine, gate or executor: plan state, events and registrations come from the
existing T10 PlanStore/Registry; observability and security come from their existing APIs
through fixed upstream clients holding the MCP host's own read credentials (the caller's
token is never forwarded). Every plan read re-checks the caller's tenant and the caller's current
per-plan `read` grant (T10 OperatorAccess), like the operator API and the console (ADR-0040).
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from bravo_platform_events.contract import validate_identifier
from bravo_platform_events.operator_access import OperatorAccess
from bravo_platform_events.store import EventError

CLIP = 4000  # Customer strings per item (Railshot diagnostics rule).
MAX_UPSTREAM_BYTES = 1 << 20
POLL_MS = {"COLLECT": 5000, "ANALYZE": 5000, "PROPOSE": 10000, "REHEARSE": 10000, "GATE": 5000,
           "SHIFT": 3000, "CUTOVER": 2000, "FAILBACK": 3000, "WATCH": 15000}
TERMINAL = {"DONE", "DONE_ROLLED_BACK"}
OBSERVABILITY_VIEWS = {"http", "database", "pgbouncer", "alerts"}
SECURITY_VIEWS = {"summary", "findings", "coverage", "sources"}


class SovaError(Exception):
    """A refusal the caller can act on; `code` is stable snake_case, never an internal reason."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def clip(value, limit=CLIP):
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit] + "…"
    if isinstance(value, list):
        return [clip(v, limit) for v in value]
    if isinstance(value, dict):
        return {k: clip(v, limit) for k, v in value.items()}
    return value


def now():
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class Upstream:
    """Fixed-origin JSON reader. The token file is the host's own credential, re-read per call."""

    base_url: str
    token_file: Path
    transport: httpx.BaseTransport | None = None
    ca_file: Path | None = None  # private CA of the upstream's server certificate (e.g. bravo-observability-api)

    def get(self, path: str, params: dict | None = None):
        if not path.startswith("/v1/") or "?" in path or "#" in path or ".." in path:
            raise SovaError("invalid_upstream_path")
        try:
            token = self.token_file.read_text().strip()
            verify = str(self.ca_file) if self.ca_file else True
            with httpx.Client(base_url=self.base_url, transport=self.transport, trust_env=False, verify=verify,
                              follow_redirects=False, timeout=5.0) as client:
                response = client.get(path, params={k: v for k, v in (params or {}).items() if v is not None},
                                      headers={"Authorization": "Bearer " + token, "Accept": "application/json"})
        except (OSError, httpx.HTTPError):
            raise SovaError("upstream_unavailable") from None
        if response.status_code in (401, 403):
            raise SovaError("upstream_forbidden")
        if response.status_code == 404:
            raise SovaError("not_found")
        if response.status_code != 200 or len(response.content) > MAX_UPSTREAM_BYTES:
            raise SovaError("upstream_unavailable")
        return response.json()


def next_actions(plan: dict, console_url: str) -> list[dict]:
    """What the server allows next for this state. Human-only effects carry a console link."""
    state, pid = plan["state"], plan["plan_id"]
    console = f"{console_url}?plan={pid}"
    follow = {"action": "follow_progress", "tool": "sova_get_progress", "requires": "sova:read"}
    if state in ("COLLECT", "ANALYZE", "PROPOSE", "REHEARSE", "GATE"):
        actions = [follow | {"why": "워크플로 호스트가 이 단계를 자동으로 진행합니다."}]
        if state in ("REHEARSE", "GATE"):
            actions.append({"action": "review_comparison", "tool": "sova_compare", "requires": "sova:read"})
        actions.append({"action": "approve_infrastructure", "requires": "human_approval", "console_url": console,
                        "why": "목적지 구축(IaC) 계획 승인은 콘솔에서 사람이 합니다."})
        return actions
    if state == "AWAIT_APPROVAL":
        return [{"action": "review_comparison", "tool": "sova_compare", "requires": "sova:read"},
                {"action": "approve", "requires": "human_approval", "console_url": console,
                 "why": "SHIFT·변경·컷오버 승인은 결속된 계획 버전으로 사람이 합니다."}]
    if state in ("SHIFT", "CUTOVER", "FAILBACK"):
        return [follow, {"action": "inspect_execution", "tool": "sova_get_execution", "requires": "sova:read"}]
    if state == "WATCH":
        return [{"action": "inspect_metrics", "tool": "sova_get_observability", "requires": "sova:read"},
                {"action": "inspect_execution", "tool": "sova_get_execution", "requires": "sova:read"},
                {"action": "approve_failback", "requires": "human_approval", "console_url": console,
                 "why": "이상이 있으면 복귀 승인·실행은 콘솔에서 사람이 합니다."}]
    if state in ("SAFE_HOLD", "NEEDS_HUMAN"):
        return [{"action": "investigate", "tool": "sova_get_progress", "requires": "sova:read",
                 "why": "자동 진행이 멈췄습니다. 마지막 이벤트로 사유를 확인합니다."},
                {"action": "decide", "requires": "human_approval", "console_url": console,
                 "why": "재개·복귀 판단은 콘솔에서 사람이 합니다."}]
    return [{"action": "review_evidence", "tool": "sova_get_execution", "requires": "sova:read"}]


@dataclass
class SovaBackend:
    store: object          # T10 PlanStore / PostgresPlanStore
    registry: object       # T10 Registry (same store)
    console_url: str
    environments: dict = field(default_factory=dict)  # tenant → environment_id → {observability, security}
    observability: Upstream | None = None
    security: Upstream | None = None
    # The T10 per-plan grants on the same store. The MCP caller is an SSO registration principal, so its
    # subject's current `read` grant is checked (requires_subject), not an operator proof.
    access: object = None

    def __post_init__(self):
        if self.access is None:
            self.access = OperatorAccess(self.store)

    def _can_read(self, c, actor, plan_row) -> bool:
        if plan_row is None or plan_row["tenant_id"] != actor.tenant_id:
            return False
        try:
            self.access.requires_subject("read")(c, dict(plan_row), actor.subject_id)
        except EventError:
            return False
        return True

    def readable(self, actor, plan_id: str) -> bool:
        """Tenant and current per-plan read grant, read in one transaction (stream checks call this per read)."""
        try:
            validate_identifier(plan_id)
        except ValueError:
            return False
        with self.store._transaction() as c:
            row = c.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
            return self._can_read(c, actor, row)

    # --- registrations and plans -------------------------------------------------------
    def _rows(self, actor):
        with self.store._transaction() as c:
            regs = c.execute("SELECT kind,object_id,receipt_json FROM registrations WHERE tenant_id=? "
                             "ORDER BY kind,object_id", (actor.tenant_id,)).fetchall()
            rows = c.execute("SELECT * FROM plans WHERE tenant_id=? ORDER BY updated_at DESC LIMIT 100",
                             (actor.tenant_id,)).fetchall()
            # Only plans the caller may read are listed (a plan without a grant is not disclosed).
            plans = [{k: row[k] for k in ("plan_id", "state", "version", "updated_at")}
                     for row in rows if self._can_read(c, actor, row)]
        out = {"environment": [], "workload": [], "plan": []}
        for row in regs:
            out.setdefault(row["kind"], []).append(json.loads(row["receipt_json"]))
        return out, plans

    def context(self, actor, options) -> dict:
        regs, plans = self._rows(actor)
        if options.plan:
            plans = [p for p in plans if p["plan_id"] == options.plan]
        return clip({
            "tenant_id": actor.tenant_id,
            "connection": {"plan": options.plan, "read_only": options.read_only},
            "allowed": ["sova:read"] + ([] if options.read_only else ["sova:register", "sova:plan"]),
            "environments": regs["environment"],
            "workloads": regs["workload"],
            "plans": plans,
            "profiles": [dict(profile_id=p, profile_version=v) for t, p, v in
                         sorted(self.registry.evaluation_profiles) if t == actor.tenant_id],
            # Operator-installed choices for the registration forms; the server still verifies each one.
            "connections": sorted(ref for t, ref in self.registry.environments if t == actor.tenant_id),
            "releases": [dict(environment_id=r.environment_id, repo_url=r.repo_url, app_path=r.app_path,
                              iac_path=r.iac_path) for (t, *_), r in sorted(self.registry.releases.items())
                         if t == actor.tenant_id],
            "console_url": self.console_url,
            "stage": plans[0]["state"] if len(plans) == 1 else None,
        })

    def plan(self, actor, plan_id: str) -> dict:
        try:
            validate_identifier(plan_id)
            if not self.readable(actor, plan_id):
                raise SovaError("forbidden")  # Same as T10: no existence disclosure, no grant no read.
            snapshot = self.store.snapshot(plan_id)
            if not self.readable(actor, plan_id):
                raise SovaError("forbidden")
        except ValueError:
            raise SovaError("invalid_plan_id") from None
        except EventError:
            raise SovaError("forbidden") from None
        return snapshot

    def progress(self, actor, plan_id: str, since: str | None, limit: int):
        self.plan(actor, plan_id)
        if not 1 <= limit <= 200:
            raise SovaError("invalid_limit")
        try:
            events = self.store.read_after(plan_id, since or None, limit=limit)
        except EventError as e:
            raise SovaError(e.code if e.code in ("invalid_cursor", "resync_required") else "forbidden") from None
        cursor = events[-1]["event_id"] if events else since
        return [clip(e) for e in events], cursor

    def events_of(self, actor, plan_id: str, types: set[str]) -> list[dict]:
        found, cursor = [], None
        while True:  # ponytail: full scan of one plan's stream; add a typed index if streams grow past ~10k.
            batch, cursor = self.progress(actor, plan_id, cursor, 200)
            found += [e for e in batch if e["event_type"] in types]
            if len(batch) < 200:
                return found

    def register(self, actor, kind: str, body: dict, key: str):
        try:
            return self.registry.register(actor, kind, body, key)
        except EventError as e:
            raise SovaError(e.code) from None
        except ValueError as e:
            # Registry ValueErrors carry a fixed code (invalid_release_path, …); anything else stays generic.
            raise SovaError(str(e) if re.fullmatch(r"[a-z_]{3,64}", str(e)) else "invalid_request") from None

    # --- observability and security (fixed upstreams) ----------------------------------
    def _environment(self, actor, environment_id: str, service: str) -> str:
        mapped = self.environments.get(actor.tenant_id, {}).get(environment_id, {}).get(service)
        if not mapped:
            raise SovaError("environment_not_connected")
        return mapped

    def read_observability(self, actor, environment_id: str, view: str):
        if self.observability is None:
            raise SovaError("observability_not_connected")
        if view not in OBSERVABILITY_VIEWS:
            raise SovaError("invalid_view")
        if view == "alerts":
            self._environment(actor, environment_id, "observability")  # tenant check only
            return self.observability.get("/v1/observability/alerts")
        name = self._environment(actor, environment_id, "observability")
        return self.observability.get(f"/v1/observability/environments/{name}/{view}")

    def read_security(self, actor, view: str, environment_id: str, params: dict):
        if self.security is None:
            raise SovaError("security_not_connected")
        if view not in SECURITY_VIEWS:
            raise SovaError("invalid_view")
        scoped = self._environment(actor, environment_id, "security")
        return clip(self.security.get(f"/v1/security/{view}", {**params, "environment_id": scoped}))
