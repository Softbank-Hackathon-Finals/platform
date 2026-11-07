"""Read current T10 action context and this actor's existing command receipts.

Native T10 routes still perform approval/dispatch. No copied gate or execution
state machine. The optional initial-proposal port uses the owner's operator code.
"""

import asyncio
import json

from .observations import ID, ObservationError, _plan
from .workspace_data import call

ACTIONS = (
    "shift",
    "failback-approve",
    "failback-submit",
    "failback-reconcile",
    "failback-edge",
    "rehearsal-start",
    "rehearsal-reconcile",
    "initial-start",
    "initial-collect",
)
PUBLIC_RECEIPT = {
    "approval_id",
    "approval_kind",
    "binding",
    "actor_id",
    "approved_at",
    "expires_at",
    "command_id",
    "rehearsal_id",
    "attempt",
    "task_id",
    "plan_version",
    "request_id",
    "status",
    "runtime_source_sha",
    "proposal_digest",
}


class T10Operations:
    def __init__(
        self,
        api,
        store,
        *,
        resolve_principal,
        authorize,
        shift=None,
        failback=None,
        rehearsal=None,
        rehearsal_tasks=None,
        initial=None,
        check_initial=None,
    ):
        if (
            not callable(resolve_principal)
            or not callable(authorize)
            or any(
                service is not None and service.store is not store
                for service in (shift, failback, rehearsal)
            )
            or (initial is not None and not callable(check_initial))
        ):
            raise ValueError("same_t10_store_and_current_permissions_required")
        self.api, self.store, self.resolve_principal, self.authorize = (
            api,
            store,
            resolve_principal,
            authorize,
        )
        self.shift, self.failback, self.rehearsal, self.initial = (
            shift,
            failback,
            rehearsal,
            initial,
        )
        self.rehearsal_tasks = rehearsal_tasks
        self.check_initial = check_initial

    async def principal(self, scope):
        from bravo_platform_events.plan_api import Principal

        actor = await call(self.resolve_principal, scope)
        if not isinstance(actor, Principal):
            raise ObservationError("forbidden", 403)
        return actor

    async def allowed(self, scope, plan, action):
        return await call(self.authorize, scope, dict(plan), action) is True

    async def context(self, scope, plan):
        actor = await self.principal(scope)
        enabled = {action: False for action in ACTIONS}
        candidates = {}
        if self.shift is not None:
            candidates["shift"] = plan["state"] == "AWAIT_APPROVAL"
        if self.failback is not None:
            candidates.update(
                {
                    "failback-approve": plan["state"] == "WATCH",
                    "failback-submit": plan["state"] == "WATCH",
                    "failback-reconcile": plan["state"]
                    in {"FAILBACK", "SAFE_HOLD", "NEEDS_HUMAN", "DONE_ROLLED_BACK"},
                    "failback-edge": plan["state"] == "FAILBACK",
                }
            )
        if self.rehearsal is not None:
            candidates.update(
                {"rehearsal-start": plan["state"] == "REHEARSE", "rehearsal-reconcile": True}
            )
        if self.initial is not None:
            candidates.update(
                {
                    "initial-start": plan["state"] == "PROPOSE",
                    "initial-collect": plan["state"] == "PROPOSE",
                }
            )
        for action, usable in candidates.items():
            enabled[action] = bool(usable and await self.allowed(scope, plan, action))
        binding, tasks = None, []
        if enabled["failback-approve"] or enabled["failback-submit"]:

            def current():
                with self.store._transaction() as connection:
                    current_plan = self.failback._plan(connection, actor, plan["plan_id"])
                    if current_plan["version"] != plan["version"]:
                        raise ValueError("plan_changed")
                    return self.failback._current(
                        connection, actor, current_plan, self.failback.clock()
                    ).binding

            try:
                binding = await asyncio.wait_for(asyncio.to_thread(current), 3)
            except Exception:
                enabled["failback-approve"] = enabled["failback-submit"] = False
        if enabled["rehearsal-start"]:
            if self.rehearsal_tasks is not None:
                try:
                    tasks = await asyncio.wait_for(call(self.rehearsal_tasks, scope, dict(plan)), 3)
                    if (
                        type(tasks) is not list
                        or len(tasks) > 32
                        or any(type(v) is not str or not ID.fullmatch(v) for v in tasks)
                    ):
                        raise ValueError("invalid_registered_tasks")
                except Exception:
                    tasks = []
            enabled["rehearsal-start"] = bool(tasks)
        command_id, runs = None, []
        if any(enabled[a] for a in ("failback-reconcile", "failback-edge", "rehearsal-reconcile")):

            def current_runs():
                from bravo_platform_events.failback import active_failback

                with self.store._transaction() as connection:
                    row = connection.execute(
                        "SELECT tenant_id,version FROM plans WHERE plan_id=?", (plan["plan_id"],)
                    ).fetchone()
                    if (
                        row is None
                        or row["tenant_id"] != actor.tenant_id
                        or row["version"] != plan["version"]
                    ):
                        raise ValueError("current_plan_required")
                    command = (
                        active_failback(connection, plan["plan_id"], plan["version"])
                        if self.failback is not None
                        else None
                    )
                    if command is None and self.failback is not None:
                        receipts = connection.execute(
                            "SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ? LIMIT 2",
                            (plan["plan_id"], "failback-command-%"),
                        ).fetchall()
                        if len(receipts) == 1:
                            command = json.loads(receipts[0]["receipt_json"])["command_id"]
                    records = (
                        connection.execute(
                            "SELECT receipt_json FROM plan_commands WHERE plan_id=? AND command_key LIKE ? ORDER BY command_key LIMIT 33",
                            (plan["plan_id"], f"rehearsal:{plan['version']}:%"),
                        ).fetchall()
                        if self.rehearsal
                        else []
                    )
                    result = [json.loads(r["receipt_json"]) for r in records]
                    if len(result) > 32:
                        raise ValueError("too_many_rehearsals")
                    return command, [
                        {k: v for k, v in r.items() if k in {"rehearsal_id", "task_id", "attempt", "plan_version"}}
                        for r in result
                    ]

            try:
                command_id, runs = await asyncio.wait_for(asyncio.to_thread(current_runs), 3)
            except Exception:
                enabled["failback-reconcile"] = enabled["failback-edge"] = enabled[
                    "rehearsal-reconcile"
                ] = False
        for action in ACTIONS:
            if enabled[action] and not await self.allowed(scope, plan, action):
                enabled[action] = False
        if not (enabled["failback-approve"] or enabled["failback-submit"]):
            binding = None
        return {
            "actions": enabled,
            "failback_binding": binding,
            "failback_command_id": command_id,
            "rehearsal_tasks": tasks,
            "rehearsal_runs": runs,
        }

    async def lookup(self, scope, plan_id, command_key):
        from bravo_platform_events.cutover import _get
        from bravo_platform_events.failback import failback_key
        from bravo_platform_events.workflow_stages import _shift_key

        if not ID.fullmatch(command_key):
            raise ObservationError("invalid_command_key", 400)
        before = await _plan(self.api, scope, plan_id)
        actor = await self.principal(scope)
        keys = {
            "shift": _shift_key("approval-key", command_key),
            "failback-approve": failback_key("approval-key", command_key),
            "failback-submit": failback_key("submit", command_key),
            "rehearsal-start": "rehearsal-key:" + command_key,
            "initial": "console-initial:" + command_key,
        }

        def read():
            found = []
            with self.store._transaction() as connection:
                for kind, key in keys.items():
                    saved = _get(connection, plan_id, key)
                    if saved is None:
                        continue
                    request, receipt = saved
                    if request.get("actor_id", request.get("subject_id")) != actor.subject_id:
                        raise ObservationError("forbidden", 403)
                    operation = request.get("operation") if kind == "initial" else kind
                    found.append(
                        (operation, {k: v for k, v in receipt.items() if k in PUBLIC_RECEIPT})
                    )
            if len(found) > 1:
                raise ObservationError("ambiguous_command_receipt")
            return found

        found = await asyncio.to_thread(read)
        if found and not await self.allowed(scope, before, found[0][0]):
            raise ObservationError("forbidden", 403)
        after = await _plan(self.api, scope, plan_id)
        if before != after:
            raise ObservationError("operation_scope_changed")
        if await self.principal(scope) != actor or (
            found and not await self.allowed(scope, after, found[0][0])
        ):
            raise ObservationError("forbidden", 403)
        return {
            "kind": "console-operation-receipt.v1",
            "plan_id": plan_id,
            "plan_digest": after["plan_digest"],
            "command_key": command_key,
            "state": "FOUND" if found else "UNKNOWN",
            "operation": found[0][0] if found else None,
            "receipt": found[0][1] if found else None,
        }

    async def initial_action(self, scope, plan_id, operation, body, key):
        from bravo_platform_events.cutover import _get, _put

        if self.initial is None or operation not in ("initial-start", "initial-collect"):
            raise ObservationError("initial_proposal_unconfigured")
        if (
            type(body) is not dict
            or set(body) != {"expected_version", "plan_digest"}
            or type(body["expected_version"]) is not int
            or not ID.fullmatch(key)
        ):
            raise ObservationError("invalid_operation_input", 400)
        before = await _plan(self.api, scope, plan_id)
        actor = await self.principal(scope)
        if not await self.allowed(scope, before, operation):
            raise ObservationError("forbidden", 403)
        request = body | {"actor_id": actor.subject_id, "operation": operation}
        ledger_key = "console-initial:" + key

        def claim():
            with self.store._transaction(write=True, plan_id=plan_id) as connection:
                # The actual authenticated T10 read above has already checked tenant access.
                row = connection.execute(
                    "SELECT * FROM plans WHERE plan_id=?", (plan_id,)
                ).fetchone()
                if row is None or row["tenant_id"] != actor.tenant_id:
                    raise ObservationError("forbidden", 403)
                if self.check_initial(connection, actor, dict(row), operation) is not True:
                    raise ObservationError("forbidden", 403)
                old = _get(connection, plan_id, ledger_key)
                if old:
                    if old[0] != request:
                        raise ObservationError("command_key_conflict", 409)
                    return old[1]
                if (
                    row["state"] != "PROPOSE"
                    or row["version"] != body["expected_version"]
                    or before["plan_digest"] != body["plan_digest"]
                ):
                    raise ObservationError("operation_scope_changed", 409)
                _put(connection, plan_id, ledger_key, request, {"status": "UNKNOWN"})
                return None

        prior = await asyncio.to_thread(claim)
        if prior is not None:
            return prior

        # Claim is durable before any Job call. A timeout/response loss is lookup-only.
        async def execute():
            try:
                result = await call(self.initial, scope, dict(before), operation)
                if type(result) is not dict or result.get("status") not in {
                    "DONE",
                    "PENDING",
                    "HOLD",
                    "FAILED",
                }:
                    raise ValueError("invalid_initial_receipt")
                receipt = {k: v for k, v in result.items() if k in PUBLIC_RECEIPT}
                json.dumps(receipt, allow_nan=False)
            except Exception:
                receipt = {"status": "UNKNOWN"}

            def persist():
                with self.store._transaction(write=True, plan_id=plan_id) as connection:
                    old = _get(connection, plan_id, ledger_key)
                    if old and old[0] == request:
                        # This is the one claimed operation's observed receipt, not a second dispatch.
                        connection.execute(
                            "UPDATE plan_commands SET receipt_json=? WHERE plan_id=? AND command_key=?",
                            (json.dumps(receipt, sort_keys=True), plan_id, ledger_key),
                        )

            await asyncio.to_thread(persist)
            return receipt

        task = asyncio.create_task(execute())
        # Retain the task until its durable readback is recorded even after an HTTP timeout.
        self._running = getattr(self, "_running", set())
        self._running.add(task)
        task.add_done_callback(self._running.discard)
        try:
            receipt = await asyncio.wait_for(asyncio.shield(task), 20)
        except TimeoutError:
            receipt = {"status": "UNKNOWN"}
        after = await _plan(self.api, scope, plan_id)
        if not await self.allowed(scope, after, operation) or await self.principal(scope) != actor:
            raise ObservationError("forbidden", 403)
        return receipt


class InitialProposalConsole:
    """Adapter over operations/initial-proposal/run.py's existing run().

    The host supplies fixed arguments/relay/client. No browser-supplied shell,
    kubeconfig, credentials, paths, model or runtime revision are accepted.
    """

    def __init__(self, *, run, arguments, relay, client):
        if not all(callable(fn) for fn in (run, arguments, relay)):
            raise ValueError("owner_operator_ports_required")
        self.run, self.arguments, self.relay, self.client = run, arguments, relay, client

    def __call__(self, scope, plan, operation):
        from bravo_agent.product_execution_contracts import ExecutionPending, ExecutionUnknown

        args = self.arguments(dict(plan), operation)
        if (
            args.plan_id != plan["plan_id"]
            or args.version != plan["version"]
            or args.command != {"initial-start": "start", "initial-collect": "collect"}[operation]
        ):
            raise ValueError("operator_plan_scope_mismatch")
        try:
            return {"status": "DONE", **self.run(args, relay=self.relay, client=self.client)}
        except ExecutionPending:
            return {"status": "PENDING"}
        except ExecutionUnknown:
            return {"status": "HOLD"}
