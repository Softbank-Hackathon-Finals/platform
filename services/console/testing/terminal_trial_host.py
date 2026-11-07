"""Checkout-only owned-trial composition beside the real observation console.

The source console remains GET-only. This separate, explicit local capability
uses the existing personal supervisor, never a product approval or shared app.
"""

import asyncio
import hashlib
import hmac
import os
import secrets
import stat
from datetime import UTC, datetime
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from bravo_console.app import HEADERS
from bravo_console.wire import object_document
from bravo_console.workload import validate_projection
from bravo_console.workspace_data import call


def private_read(path, limit=120000):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("protected_trial_file_required")
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("trial_size_limit")
    return raw


def canonical(value):
    import json

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


class TerminalTrialHost:
    def __init__(self, console, manager, *, authorize, terminal_command):
        self.console, self.manager, self.authorize = console, manager, authorize
        self.csrf = secrets.token_urlsafe(32)
        self.terminal_command = terminal_command
        if not callable(authorize) or not terminal_command or "\n" in terminal_command:
            raise ValueError("explicit_owned_trial_composition_required")
        assets = Path(__file__).with_name("terminal_trial_assets")
        self.assets = {
            "/__trial/client.js": ((assets / "client.js").read_bytes(), "application/javascript"),
            "/__trial/style.css": ((assets / "style.css").read_bytes(), "text/css"),
        }
        body, mime = console.assets["/console/"]
        console.assets["/console/"] = (
            body.replace(
                b"</body>", b'<script type="module" src="/__trial/client.js"></script></body>'
            ),
            mime,
        )

    def status(self):
        return self.manager.status() | {
            "csrf": self.csrf,
            "terminal_command": self.terminal_command,
            "qualification_only": True,
            "authorizes_execution": False,
        }

    def feed(self):
        status = self.manager.status()
        if not status["session_id"] or not self.manager.session:
            raise ValueError("trial_not_prepared")
        root = self.manager.session / "state"
        binding = object_document(
            private_read(root / "registration.private.json"), max_bytes=120000
        )
        metadata = object_document(private_read(root / "trial.private.json"), max_bytes=120000)
        key = private_read(root / "feed-key.private", 32)
        if (
            len(key) != 32
            or metadata["source_revision"] != self.manager.settings.producer_revision
            or metadata["qualification_only"] is not True
        ):
            raise ValueError("unregistered_trial")
        envelope = object_document(private_read(root / "feed.private.json"), max_bytes=120000)
        if (
            set(envelope) != {"producer_id", "source_revision", "body", "hmac_sha256"}
            or envelope["producer_id"] != "owned-onprem-qualification"
            or envelope["source_revision"] != self.manager.settings.producer_revision
        ):
            raise ValueError("unregistered_feed")
        signature = envelope.pop("hmac_sha256")
        expected = hmac.new(key, canonical(envelope), hashlib.sha256).hexdigest()
        if type(signature) is not str or not hmac.compare_digest(signature, expected):
            raise ValueError("unverified_feed")
        body = envelope["body"]
        if body.get("provenance") != "live":
            raise ValueError("actual_owned_trial_feed_required")
        plan = {k: binding[k] for k in ("plan_id", "plan_digest", "plan_version")}
        plan["version"] = plan.pop("plan_version")
        validate_projection(body, plan, datetime.now(UTC))
        if private_read(root / "feed-key.private", 32) != key:
            raise ValueError("trial_key_changed")
        if self.manager.status()["session_id"] != status["session_id"]:
            raise ValueError("trial_session_changed")
        return {
            "qualification_only": True,
            "plan_connected": False,
            "authorizes_execution": False,
            "session_id": status["session_id"],
            "document": body,
        }

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/__trial/"):
            return await self.console(scope, receive, send)
        request = Request(scope, receive)

        async def answer(status, value):
            return await JSONResponse(value, status_code=status, headers=HEADERS)(
                scope, receive, send
            )

        if (
            request.headers.getlist("host") != [self.console.host]
            or request.headers.get("sec-fetch-site") == "cross-site"
            or (
                "origin" in request.headers
                and request.headers.getlist("origin") != [self.console.origin]
            )
            or scope.get("query_string")
        ):
            return await answer(403, {"error": "trial_origin_denied"})
        if await call(self.authorize, scope) is not True:
            return await answer(403, {"error": "trial_session_required"})
        path = scope["path"]
        if request.method == "GET" and path in self.assets:
            body, mime = self.assets[path]
            return await Response(body, media_type=mime, headers=HEADERS)(scope, receive, send)
        try:
            if request.method == "GET" and path in {"/__trial/status", "/__trial/feed"}:
                value = await asyncio.wait_for(
                    asyncio.to_thread(self.status if path.endswith("status") else self.feed),
                    timeout=5,
                )
                if await call(self.authorize, scope) is not True:
                    return await answer(403, {"error": "trial_session_required"})
                return await answer(200, value)
            if request.method != "POST" or path != "/__trial/control":
                return await answer(404, {"error": "not_found"})
            if (
                request.headers.getlist("origin") != [self.console.origin]
                or request.headers.getlist("content-type") != ["application/json"]
                or request.headers.getlist("x-sova-trial") != [self.csrf]
                or len(request.headers.getlist("content-length")) != 1
                or "transfer-encoding" in request.headers
            ):
                return await answer(403, {"error": "trial_csrf_required"})
            length = int(request.headers["content-length"])
            if not 0 < length <= 2048:
                raise ValueError("bounded_request_required")
            raw = bytearray()
            async with asyncio.timeout(3):
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > 2048:
                        raise ValueError("bounded_request_required")
            if len(raw) != length:
                raise ValueError("request_length_mismatch")
            value = object_document(raw, max_bytes=2048)
            if value.get("action") not in {"prepare", "respond", "stop"}:
                raise ValueError("fixed_trial_action_required")
            if await call(self.authorize, scope) is not True:
                return await answer(403, {"error": "trial_session_required"})
            # Manager performs current scope/explicit approval checks again at
            # dispatch. No timeout retry can create a second operation.
            accepted = await asyncio.to_thread(self.manager.act, value)
            return await answer(202, accepted)
        except ValueError:
            return await answer(409, {"error": "trial_scope_or_action_not_current"})
        except Exception:
            return await answer(503, {"error": "trial_unconfirmed", "retry_automatically": False})
