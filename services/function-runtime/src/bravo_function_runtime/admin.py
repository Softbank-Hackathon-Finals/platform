"""정지 스위치와 상태 확인 HTTP 서버.

Cutover Executor가 demo-chat에 쓰는 것과 같은 계약이다(ADR-0047): Pod 안에서
`GET|PUT http://127.0.0.1:8080/admin/write-block`, `Authorization: Bearer $ADMIN_TOKEN`, PUT 본문 `{"blocked": bool}`,
응답 `{"write_blocked": bool, "in_flight": int}`. 토큰이 설정되지 않으면 관리 요청을 모두 거부한다.
"""

from __future__ import annotations

import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .runner import Runner


def serve(runner: Runner, port: int, token: str | None, host: str = "0.0.0.0") -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # 요청 로그에 토큰이 남지 않게 기본 로그를 끈다
            pass

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            given = self.headers.get("Authorization", "")
            return bool(token) and hmac.compare_digest(given.encode(), f"Bearer {token}".encode())

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, {"ok": True})
            if self.path == "/admin/write-block":
                if not self._authorized():
                    return self._send(403, {"detail": "forbidden"})
                return self._send(200, runner.status())
            return self._send(404, {"detail": "not found"})

        def do_PUT(self):
            if self.path != "/admin/write-block":
                return self._send(404, {"detail": "not found"})
            if not self._authorized():
                return self._send(403, {"detail": "forbidden"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                blocked = json.loads(self.rfile.read(length) or b"{}")["blocked"]
            except (ValueError, KeyError):
                return self._send(400, {"detail": "body must be {\"blocked\": bool}"})
            if not isinstance(blocked, bool):
                return self._send(400, {"detail": "blocked must be a boolean"})
            return self._send(200, runner.set_blocked(blocked))

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, name="admin", daemon=True).start()
    return server
