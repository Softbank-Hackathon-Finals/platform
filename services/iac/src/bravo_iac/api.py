"""Read-and-propose HTTP API over the catalog and one environment's desired state in Git.

    GET  /v1/iac/modules                     the module catalog
    GET  /v1/iac/environments/{environment}  the desired state as committed, with its digest
    GET  /v1/iac/environments/{environment}/node-files         files its nodes must hold (node.py)
    GET  /v1/iac/environments/{environment}/node-files/{path}  one of them, with its exact content
    GET  /v1/iac/network                     every environment's tailnet routes and policy fragments (network.py)
    POST /v1/iac/plan                        {"environment", "proposed", "origin"} -> iac-plan.v1

It reads the committed files only. It never writes Git, never applies to a cluster and never
holds credentials; the host authenticates callers in front of it.
"""

import json
from pathlib import Path

from . import catalog, network, node
from .modules import IacError
from .plan import digest, plan


def create_app(environments_dir):
    from fastapi import Body, FastAPI, HTTPException

    root = Path(environments_dir)
    app = FastAPI(title="bravo-iac", docs_url=None, redoc_url=None, openapi_url=None)

    def committed(environment):
        if not isinstance(environment, str) or not catalog.ENVIRONMENT_RE.fullmatch(environment):
            raise HTTPException(404, {"code": "IAC_ENVIRONMENT_UNKNOWN"})
        path = root / f"{environment}.json"
        if not path.is_file():
            raise HTTPException(404, {"code": "IAC_ENVIRONMENT_UNKNOWN"})
        try:
            state = catalog.check_state(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, IacError):
            raise HTTPException(503, {"code": "IAC_STATE_UNREADABLE"}) from None
        if state["environment"] != environment:
            raise HTTPException(503, {"code": "IAC_STATE_UNREADABLE"})
        return state

    @app.get("/v1/iac/modules")
    def modules():
        return {"modules": catalog.describe()}

    @app.get("/v1/iac/environments/{environment}")
    def environment(environment: str):
        state = committed(environment)
        return {"state": state, "digest": digest(state)}

    @app.get("/v1/iac/environments/{environment}/node-files")
    def node_files(environment: str):
        return node.listing(committed(environment))

    @app.get("/v1/iac/environments/{environment}/node-files/{path:path}")
    def node_file(environment: str, path: str):
        try:
            return node.read(committed(environment), "/" + path)
        except IacError as error:
            raise HTTPException(404, {"code": error.code}) from None

    @app.get("/v1/iac/network")
    def network_view():
        states = [committed(path.stem) for path in sorted(root.glob("*.json"))]
        try:
            return network.view(states)
        except IacError as error:
            raise HTTPException(503, {"code": error.code}) from None

    @app.post("/v1/iac/plan")
    def make_plan(body: dict = Body(...)):  # noqa: B008 - FastAPI body declaration
        if type(body) is not dict or set(body) != {"environment", "proposed", "origin"}:
            raise HTTPException(422, {"code": "IAC_REQUEST_INVALID"})
        try:
            return plan(committed(body["environment"]), body["proposed"], origin=body["origin"])
        except IacError as error:
            raise HTTPException(422, {"code": error.code}) from None

    return app
