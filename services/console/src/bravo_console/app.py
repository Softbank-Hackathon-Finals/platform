"""Serve the console beside a host-authenticated T10 ASGI application.

The host owns verified session identity, tenant access and all change permissions.
This wrapper adds same-origin browser protections, never authentication by header.
"""

import re
from pathlib import Path
from urllib.parse import urlsplit

from starlette.responses import JSONResponse, Response

from .observations import ObservationError

ASSETS = {
    "/console/": ("index.html", "text/html"),
    "/console/app.js": ("app.js", "text/javascript"),
    "/console/model.js": ("model.js", "text/javascript"),
    "/console/transport.js": ("transport.js", "text/javascript"),
    "/console/recovery.js": ("recovery.js", "text/javascript"),
    "/console/registration.js": ("registration.js", "text/javascript"),
    "/console/workspace.js": ("workspace.js", "text/javascript"),
    "/console/style.css": ("style.css", "text/css"),
}
HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'",
}


class ConsoleApp:
    def __init__(self, api, *, public_origin: str, observations=None, controls=None, security=None):
        parsed = urlsplit(public_origin)
        if (
            parsed.scheme not in ("https", "http")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path
            or (
                parsed.scheme == "http"
                and parsed.hostname not in ("localhost", "127.0.0.1", "[::1]", "::1")
            )
        ):
            raise ValueError("fixed_secure_console_origin_required")
        self.api, self.origin, self.host = api, public_origin, parsed.netloc
        self.observations = observations
        self.controls = controls
        self.security = security
        self.assets = {
            path: ((Path(__file__).parent / "static" / name).read_bytes(), mime)
            for path, (name, mime) in ASSETS.items()
        }

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.api(scope, receive, send)
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            return
        pairs = scope.get("headers", [])
        headers = {k.lower(): v for k, v in pairs}
        path, method = scope["path"], scope["method"]

        async def reject(status, code):
            await JSONResponse({"detail": {"code": code}}, status_code=status, headers=HEADERS)(
                scope, receive, send
            )

        for key in (b"host", b"origin", b"content-type", b"content-length", b"x-bravo-console"):
            if sum(k.lower() == key for k, _ in pairs) > 1:
                return await reject(400, "ambiguous_headers")
        if headers.get(b"host", b"").decode("latin1") != self.host:
            return await reject(421, "unexpected_host")
        if headers.get(b"sec-fetch-site") == b"cross-site":
            return await reject(403, "cross_site_request")
        if b"origin" in headers and headers[b"origin"].decode("latin1") != self.origin:
            return await reject(403, "origin_mismatch")
        if path in ASSETS:
            if method not in ("GET", "HEAD"):
                return await reject(405, "method_not_allowed")
            body, mime = self.assets[path]
            await Response(body if method == "GET" else b"", media_type=mime, headers=HEADERS)(
                scope, receive, send
            )
            return
        registration = path in ("/v1/environments", "/v1/workloads", "/v1/plans")
        if not registration and not path.startswith("/v1/plans/"):
            return await reject(404, "not_found")
        if registration and method != "POST":
            return await reject(405, "method_not_allowed")
        if method not in ("GET", "POST"):
            return await reject(405, "method_not_allowed")
        if match := re.fullmatch(r"/v1/plans/([A-Za-z0-9][A-Za-z0-9_-]{0,127})/observations", path):
            if method != "GET":
                return await reject(405, "method_not_allowed")
            if self.observations is None:
                return await reject(503, "observations_unconfigured")
            try:
                value = await self.observations.read(scope, match[1])
            except ObservationError as error:
                return await reject(error.status, error.code)
            except Exception:
                return await reject(503, "observations_unavailable")
            return await JSONResponse(value, headers=HEADERS)(scope, receive, send)
        if match := re.fullmatch(r"/v1/plans/([A-Za-z0-9][A-Za-z0-9_-]{0,127})/controls", path):
            if method != "GET":
                return await reject(405, "method_not_allowed")
            if self.controls is None:
                return await reject(503, "control_report_unconfigured")
            try:
                value = await self.controls.read(scope, match[1])
            except ObservationError as error:
                return await reject(error.status, error.code)
            except Exception:
                return await reject(503, "control_report_unavailable")
            return await JSONResponse(value, headers=HEADERS)(scope, receive, send)
        if match := re.fullmatch(r"/v1/plans/([A-Za-z0-9][A-Za-z0-9_-]{0,127})/security", path):
            if method != "GET":
                return await reject(405, "method_not_allowed")
            if scope.get("query_string"):
                return await reject(400, "security_query_not_allowed")
            if self.security is None:
                return await reject(503, "security_unconfigured")
            try:
                value = await self.security.read(scope, match[1])
            except ObservationError as error:
                return await reject(error.status, error.code)
            except Exception:
                return await reject(503, "security_unavailable")
            return await JSONResponse(value, headers=HEADERS)(scope, receive, send)
        if method == "POST":
            if (
                headers.get(b"origin", b"").decode("latin1") != self.origin
                or headers.get(b"x-bravo-console") != b"1"
                or headers.get(b"content-type", b"").split(b";")[0].strip() != b"application/json"
            ):
                return await reject(403, "browser_change_boundary")
            chunks, length = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                part = message.get("body", b"")
                length += len(part)
                if length > 65536:
                    return await reject(413, "request_too_large")
                chunks.append(part)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            original_receive = receive
            delivered = False

            async def buffered_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await original_receive()

            receive = buffered_receive

        async def protected_send(message):
            if message["type"] == "http.response.start":
                outgoing = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower().decode("latin1") not in {h.lower() for h in HEADERS}
                ]
                outgoing.extend((k.lower().encode(), v.encode()) for k, v in HEADERS.items())
                message = dict(message, headers=outgoing)
            await send(message)

        await self.api(scope, receive, protected_send)
