"""T44 HTTP API (ADR-0032 3.3). Operators, the console and the agent use the same routes.

- Plans: anyone authenticated may create and read one. Creating a plan never writes to a cluster
  or the configuration repository; it reads the live target identity and repository head.
- Runs and operator confirmations: humans only. An agent principal is refused here, in code.
- Approval is not here: it is T10 `approval_kind=infra`. A run forwards the caller's bearer token
  to T10, so T10 checks the caller's own `apply_infra` grant when the run claims the approval.
"""

import json
import threading
import uuid
from dataclasses import dataclass

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .stack import IDENTIFIER, StackError, canonical


@dataclass(frozen=True)
class Principal:
    subject_id: str
    kind: str  # "human" | "agent"
    token: str


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    migration_plan_id: str = Field(pattern=IDENTIFIER)
    approval_id: str = Field(pattern=IDENTIFIER)


def plan_view(plan_id, document, requested_by, stack):
    steps = stack.steps
    return {
        "infra_plan_id": plan_id, "plan_digest": document["plan_digest"], "requested_by": requested_by,
        "stack": document["stack"], "stack_revision": document["stack_revision"],
        "base_commit": document["base_commit"], "target": document["target"], "levels": document["levels"],
        "steps": [{k: s[k] for k in ("id", "kind", "owner", "needs", "digest")} | (
            {"files": s["files"]} if "files" in s else {}) for s in document["steps"]],
        "operator_actions": [{"id": s["id"], "instruction": steps[s["id"]]["instruction"]}
                             for s in document["steps"] if s["kind"] == "operator_action"],
        "binding": {"infra_plan_id": plan_id, "plan_digest": document["plan_digest"], "requested_by": requested_by},
    }


def run_view(run):
    return {k: run.get(k) for k in ("run_id", "status", "reason", "plan_digest", "migration_plan_id", "approval_id",
                                     "started_by", "created_at", "updated_at", "commits", "confirmations")} | {
        "steps": run["steps"]}


def create_app(*, stack, plans, executor_for, resolve_principal):
    """`plans` stores plan documents; `executor_for(token)` builds an Executor whose T10 client
    carries the caller's token. `resolve_principal(request)` returns a Principal or None."""
    app = FastAPI(title="bravo-infra-plan", docs_url=None, redoc_url=None, openapi_url=None)
    lock = threading.Lock()
    active = {}  # run_id -> thread; one executor per run at a time

    def launch(key, target, *args, **kwargs):
        with lock:
            if key in active and active[key].is_alive():
                raise StackError("run_busy")
            active[key] = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
            active[key].start()

    def principal(request, *, human=False):
        who = resolve_principal(request)
        if not isinstance(who, Principal):
            raise HTTPException(401, detail={"code": "unauthenticated"})
        if human and who.kind != "human":
            raise HTTPException(403, detail={"code": "human_required"})
        return who

    @app.exception_handler(RequestValidationError)
    async def invalid(_request, _error):
        return JSONResponse({"detail": {"code": "invalid_request"}}, status_code=422)

    @app.exception_handler(StackError)
    async def refused(_request, error):
        code = str(error)
        status = 404 if code in ("run_unavailable", "plan_unavailable") else 409
        if code in ("invalid_plan_request",):
            status = 422
        return JSONResponse({"detail": {"code": code}}, status_code=status)

    @app.get("/v1/infra/request-schema")
    def request_schema(request: Request):
        principal(request)
        return stack.request_schema()

    @app.post("/v1/infra/plans", status_code=201)
    async def create_plan(request: Request):
        who = principal(request)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            raise StackError("invalid_plan_request") from None
        executor = executor_for(who.token)
        target = executor.cluster.identity(stack.validate_request(body)["params"]["node_name"])
        plan = executor.planner.plan(body, stack_revision=executor.stack_revision, target=target,
                                     base_commit=executor.repo.head())
        plan_id = "infra-" + uuid.uuid4().hex[:20]
        plans.save(plan_id, {"document": plan.document, "requested_by": who.subject_id,
                             "requested_kind": who.kind})
        return plan_view(plan_id, plan.document, who.subject_id, stack)

    @app.get("/v1/infra/plans/{plan_id}")
    def read_plan(plan_id: str, request: Request):
        principal(request)
        saved = plans.load(plan_id)
        return plan_view(plan_id, saved["document"], saved["requested_by"], stack)

    @app.post("/v1/infra/plans/{plan_id}/runs", status_code=202)
    def start_run(plan_id: str, body: RunInput, request: Request, idempotency_key: str = Header(max_length=128)):
        who = principal(request, human=True)
        saved = plans.load(plan_id)
        run_id = "run-" + uuid.uuid5(uuid.NAMESPACE_URL, canonical([plan_id, idempotency_key])).hex[:20]
        executor = executor_for(who.token)
        if executor.store.exists(run_id):
            return run_view(executor.store.load(run_id))
        # The executor re-renders and refuses unless the result equals the approved digest.
        launch(run_id, executor.start, request=saved["document"]["request"],
               migration_plan_id=body.migration_plan_id, approval_id=body.approval_id, run_id=run_id,
               started_by=who.subject_id)
        return {"run_id": run_id, "status": "ACCEPTED"}

    @app.get("/v1/infra/runs/{run_id}")
    def read_run(run_id: str, request: Request):
        who = principal(request)
        return run_view(executor_for(who.token).store.load(run_id))

    @app.post("/v1/infra/runs/{run_id}/steps/{step_id}/confirm", status_code=202)
    def confirm(run_id: str, step_id: str, request: Request):
        who = principal(request, human=True)
        executor = executor_for(who.token)
        executor.store.load(run_id)
        if step_id not in stack.steps or stack.steps[step_id]["kind"] != "operator_action":
            raise StackError("not_an_operator_step")
        launch(run_id, executor.confirm, run_id, step_id, confirmed_by=who.subject_id)
        return {"run_id": run_id, "step": step_id, "status": "ACCEPTED"}

    return app
