"""Sova's original shell in a strictly read-only observation mode.

Authentication is supplied by the host. No operational plan, approval, grant,
registration endpoint, environment enrollment, or execution route is exposed.
"""

import asyncio
from pathlib import Path
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .app import ASSETS, HEADERS
from .observed_workload import ObservedWorkloads
from .workspace_data import call


class ObservationConsole:
    def __init__(self, source, *, origin, authorize):
        parsed = urlsplit(origin)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or not parsed.port
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or type(source) is not ObservedWorkloads
            or not callable(authorize)
        ):
            raise ValueError("explicit_loopback_observation_host_required")
        self.source, self.authorize, self.origin, self.host = (
            source,
            authorize,
            origin,
            parsed.netloc,
        )
        self.assets = {
            path: ((Path(__file__).parent / "static" / name).read_bytes(), mime)
            for path, (name, mime) in ASSETS.items()
        }
        html = (
            self.assets["/console/"][0]
            .decode()
            .replace("<body>", '<body data-observation-only="true">')
        )
        html = html.replace("/console/app.js", "/console/observation-app.js")
        html = html.replace(
            "</head>", '<link rel="stylesheet" href="/console/observation.css" /></head>'
        )
        self.assets["/console/"] = (html.encode(), "text/html")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            task = None
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    task = asyncio.create_task(self.source.poll())
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    if task:
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4403})
            return
        request = Request(scope, receive)

        async def error(status, code):
            return await JSONResponse(
                {"detail": {"code": code}}, status_code=status, headers=HEADERS
            )(scope, receive, send)

        if (
            request.headers.getlist("host") != [self.host]
            or request.headers.get("sec-fetch-site") == "cross-site"
            or ("origin" in request.headers and request.headers.getlist("origin") != [self.origin])
        ):
            return await error(403, "observation_origin_denied")
        if request.method not in {"GET", "HEAD"}:
            return await error(405, "observation_is_read_only")
        if scope.get("query_string"):
            return await error(400, "observation_query_not_allowed")
        path = scope["path"]
        if path in self.assets:
            body, mime = self.assets[path]
            return await Response(
                body if request.method == "GET" else b"", media_type=mime, headers=HEADERS
            )(scope, receive, send)
        if path != "/v1/observed-workload":
            return await error(404, "not_found")
        try:
            if await call(self.authorize, scope) is not True:
                return await error(403, "observation_session_required")
            value = self.source.read()
            if await call(self.authorize, scope) is not True:
                return await error(403, "observation_session_required")
        except Exception:
            return await error(503, "observation_unconfirmed")
        return await JSONResponse(value, headers=HEADERS)(scope, receive, send)
