"""Loopback operator client: existing personal SSO proofs stay outside the browser.

This is an internal CLI access path, not a public web identity provider. The
platform still verifies each short proof and current plan permissions itself.
"""

import asyncio
import re
import secrets
import ssl
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse


class OperatorBridge:
    def __init__(
        self,
        *,
        origin,
        upstream,
        tls_context,
        proof,
        tls_server_name=None,
        transport=None,
        clock=None,
    ):
        local, target = urlsplit(origin), urlsplit(upstream)
        tls_server_name = tls_server_name if tls_server_name is not None else target.hostname
        if (
            local.scheme != "http"
            or local.hostname != "127.0.0.1"
            or not local.port
            or local.path
            or local.query
            or local.fragment
            or local.username
            or target.scheme != "https"
            or not target.hostname
            or target.path
            or target.query
            or target.fragment
            or target.username
            or not callable(proof)
            or not isinstance(tls_context, ssl.SSLContext)
            or tls_context.verify_mode != ssl.CERT_REQUIRED
            or not tls_context.check_hostname
            or not isinstance(tls_server_name, str)
            or len(tls_server_name) > 253
            or any(
                not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
                for label in tls_server_name.split(".")
            )
        ):
            raise ValueError("fixed_operator_origins_required")
        self.origin, self.upstream, self.host = origin, upstream, local.netloc
        self.proof, self.tls = proof, tls_context
        self.tls_server_name = tls_server_name
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(UTC).timestamp())
        self.started = self.clock()
        self.bootstrap = secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        self.bootstrap_used = False
        self.lock = asyncio.Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                event = await receive()
                if event["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif event["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4404})
            return
        request = Request(scope, receive)
        safe = {
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }

        async def reject(status, code):
            return await JSONResponse({"detail": {"code": code}}, status_code=status, headers=safe)(
                scope, receive, send
            )

        if (
            request.headers.getlist("host") != [self.host]
            or request.headers.get("sec-fetch-site") == "cross-site"
            or "authorization" in request.headers
            or request.method not in ("GET", "POST")
        ):
            return await reject(403, "operator_bridge_boundary")
        if scope["path"] == "/_operator/" + self.bootstrap:
            async with self.lock:
                if (
                    self.bootstrap_used
                    or request.method != "GET"
                    or not 0 <= self.clock() - self.started < 300
                ):
                    return await reject(403, "operator_bootstrap_expired")
                self.bootstrap_used = True
            response = RedirectResponse("/console/", status_code=303, headers=safe)
            response.set_cookie(
                "sova-operator",
                self.session,
                httponly=True,
                samesite="strict",
                max_age=900,
                path="/",
            )
            return await response(scope, receive, send)
        cookie = request.cookies.get("sova-operator", "")
        if (
            not cookie.isascii()
            or not secrets.compare_digest(cookie, self.session)
            or not 0 <= self.clock() - self.started < 900
        ):
            return await reject(401, "operator_session_required")
        path = scope["path"]
        if not (
            path.startswith("/console/")
            or path.startswith("/v1/plans/")
            or path in ("/v1/plans", "/v1/environments", "/v1/workloads")
        ):
            return await reject(404, "not_found")
        if request.method == "POST" and (
            request.headers.getlist("origin") != [self.origin]
            or request.headers.getlist("x-bravo-console") != ["1"]
        ):
            return await reject(403, "browser_change_boundary")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                return await reject(413, "request_too_large")
        headers = {"Host": self.host}
        for name in (
            "accept",
            "content-type",
            "origin",
            "x-bravo-console",
            "idempotency-key",
            "last-event-id",
        ):
            values = request.headers.getlist(name)
            if len(values) > 1:
                return await reject(400, "duplicate_header")
            if values:
                headers[name] = values[0]
        client = None
        try:
            # No client token/body can select a profile, tenant or proof audience.
            headers["Authorization"] = "Bearer " + await asyncio.to_thread(self.proof)
            client = httpx.AsyncClient(
                verify=self.tls,
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(15, read=55),
            )
            query = scope.get("query_string", b"").decode("ascii")
            url = self.upstream + path + ("?" + query if query else "")
            upstream = await client.send(
                client.build_request(
                    request.method,
                    url,
                    headers=headers,
                    content=bytes(body),
                    extensions={"sni_hostname": self.tls_server_name},
                ),
                stream=True,
            )
            if 300 <= upstream.status_code < 400:
                await upstream.aclose()
                await client.aclose()
                return await reject(502, "upstream_redirect_refused")
            response_headers = {
                k: v
                for k, v in upstream.headers.items()
                if k.lower()
                in {
                    "content-type",
                    "cache-control",
                    "pragma",
                    "referrer-policy",
                    "content-security-policy",
                    "x-content-type-options",
                    "x-frame-options",
                    "permissions-policy",
                }
            }
            if upstream.headers.get("content-type", "").startswith("text/event-stream"):

                async def events():
                    iterator = upstream.aiter_bytes()
                    end = asyncio.get_running_loop().time() + 45
                    try:
                        while True:
                            left = end - asyncio.get_running_loop().time()
                            if left <= 0:
                                return
                            try:
                                chunk = await asyncio.wait_for(anext(iterator), left)
                            except (StopAsyncIteration, TimeoutError):
                                return
                            yield chunk
                    except httpx.HTTPError:
                        return
                    finally:
                        await upstream.aclose()
                        await client.aclose()

                # End normally before the 60s proof expires; the existing console
                # reconnect obtains a new proof without putting it in JS/storage.
                return await StreamingResponse(
                    events(), status_code=upstream.status_code, headers=response_headers
                )(scope, receive, send)
            data = bytearray()
            async for chunk in upstream.aiter_bytes():
                data.extend(chunk)
                if len(data) > 8 * 1024 * 1024:
                    raise ValueError("upstream_response_limit")
            await upstream.aclose()
            await client.aclose()
            client = None
            return await Response(
                bytes(data), status_code=upstream.status_code, headers=response_headers
            )(scope, receive, send)
        except Exception:
            if client is not None:
                await client.aclose()
            return await reject(503, "operator_upstream_unavailable")


def main(argv=None):
    """Open an internal CLI session with the existing personal AWS SSO identity."""
    import argparse
    import json
    import os
    import webbrowser
    from pathlib import Path

    import uvicorn
    from bravo_platform_events.operator_identity import OPERATOR_SSO_AUDIENCE
    from bravo_platform_events.registration_sso import make_registration_token

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="protected, deployment-owned JSON file")
    parser.add_argument(
        "--session-file", required=True, help="new private file for the one-use URL"
    )
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = json.loads(Path(args.config).read_text())
        if set(config) != {"origin", "upstream", "ca_file", "tls_server_name"}:
            raise ValueError("fixed_operator_configuration_required")
        tls = ssl.create_default_context(cafile=config["ca_file"])

        def proof():
            return make_registration_token(
                profile_name="bravo-personal",
                region="ap-northeast-2",
                audience=OPERATOR_SSO_AUDIENCE,
            )

        app = OperatorBridge(
            origin=config["origin"],
            upstream=config["upstream"],
            tls_context=tls,
            proof=proof,
            tls_server_name=config["tls_server_name"],
        )
        # Refuse an occupied port before consuming or exposing the one-use URL.
        import socket

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", urlsplit(app.origin).port))
        listener.listen(16)
        url = app.origin + "/_operator/" + app.bootstrap
        fd = os.open(args.session_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            output.write(url + "\n")
        if args.open_browser:
            webbrowser.open(url)
        print("운영자 접속 주소를 비공개 세션 파일에 저장했습니다. 세션은 최대 15분입니다.")
        server = uvicorn.Server(
            uvicorn.Config(app, access_log=False, proxy_headers=False, server_header=False)
        )
        asyncio.run(server.serve(sockets=[listener]))
    except (OSError, ValueError, KeyError):
        raise SystemExit("operator_bridge_configuration_unavailable") from None


if __name__ == "__main__":
    main()
