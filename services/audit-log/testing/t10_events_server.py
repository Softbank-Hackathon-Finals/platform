"""Run the real T10 event API for the follow tests and the live check.

The T10 source (``services/platform-events/src``) is imported as is: ``create_app`` and its
SQLite ``EventStore``/``PlanStore``. Nothing is copied or changed. T10 leaves authorization to
the deployment (an ``Authorizer(request, plan_id)`` predicate); this harness injects a bearer
check against one token, the way a deployment would inject its own.
"""

from __future__ import annotations

import hmac
import socket
import sys
import threading
import time
from pathlib import Path

T10_SRC = Path(__file__).resolve().parents[2] / "platform-events" / "src"


def t10():
    """The real T10 modules (api, store, plans)."""
    if str(T10_SRC) not in sys.path:
        sys.path.insert(0, str(T10_SRC))
    from bravo_platform_events import api, plans, store

    return api, store, plans


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class T10Server:
    """``create_app(store, authorize=…)`` under uvicorn on a loopback port, in a thread."""

    def __init__(self, store, token: str, *, poll_seconds: float = 0.05):
        import uvicorn

        api, _store, _plans = t10()
        self.token = token
        self.presented: list[str | None] = []  # Authorization values seen (tests only)
        self.cursors: list[str | None] = []  # Last-Event-ID of each authorized request
        self.port = free_port()
        app = api.create_app(store, authorize=self._authorize, poll_seconds=poll_seconds)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning",
                                lifespan="off", timeout_graceful_shutdown=1)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def _authorize(self, request, _plan_id: str) -> bool:
        presented = request.headers.get("authorization")
        ok = presented is not None and hmac.compare_digest(presented, f"Bearer {self.token}")
        # T10 re-checks access during a stream; record each request once.
        if not request.scope.setdefault("state", {}).get("t24_seen"):
            request.scope["state"]["t24_seen"] = True
            self.presented.append(presented)
            if ok:
                self.cursors.append(request.headers.get("last-event-id"))
        return ok

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> T10Server:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("T10 server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *_exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)
