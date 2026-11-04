#!/usr/bin/env python3
"""Edge 대역 뒤의 앱을 0.5초 죽였을 때 502가 얼마나 이어지는지 잰다. 컨테이너 lock-lab-edge-blip 하나만 쓰고 지운다.

    python3 edge_blip.py <산출물 폴더>
"""

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

CONTAINER = "lock-lab-edge-blip"
SERVER = """
import http.server, socketserver, sys
class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *args):
        pass
class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
Server(("0.0.0.0", int(sys.argv[1])), Handler).serve_forever()
"""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))
        return sock.getsockname()[1]


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    port = free_port()

    def serve() -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-c", SERVER, str(port)])

    conf = out / "edge-blip.conf"
    # testing/mock-edge.conf의 upstream과 같은 모양이다.
    conf.write_text(f"""
upstream target {{ server host.docker.internal:{port}; keepalive 64; }}
server {{
    listen 8080;
    access_log off;
    location / {{
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_pass http://target;
        error_page 502 504 = @edge_error;
    }}
    location @edge_error {{ default_type text/plain; return 502 "edge error\\n"; }}
}}
""")
    subprocess.run(["docker", "create", "--name", CONTAINER, "--add-host", "host.docker.internal:host-gateway", "-p", "127.0.0.1::8080", "nginx:alpine"], check=True, capture_output=True)
    server = serve()
    try:
        subprocess.run(["docker", "cp", str(conf), f"{CONTAINER}:/etc/nginx/conf.d/default.conf"], check=True)
        subprocess.run(["docker", "start", CONTAINER], check=True, capture_output=True)
        time.sleep(1)
        edge = subprocess.run(["docker", "port", CONTAINER, "8080"], capture_output=True, text=True, check=True).stdout.strip()

        def status() -> int:
            try:
                return urllib.request.urlopen(f"http://{edge}/", timeout=3).status
            except urllib.error.HTTPError as error:
                return error.code
            except OSError:
                return 0

        results: list[tuple[float, int]] = []
        started = time.monotonic()
        running = threading.Event()
        running.set()

        def load() -> None:
            while running.is_set():
                at = time.monotonic() - started
                threading.Thread(target=lambda at=at: results.append((round(at, 2), status())), daemon=True).start()
                time.sleep(0.05)

        threading.Thread(target=load, daemon=True).start()
        time.sleep(3)
        server.kill()
        server.wait()
        down_at = time.monotonic() - started
        time.sleep(0.5)
        server = serve()
        up_at = time.monotonic() - started
        time.sleep(14)
        running.clear()
        time.sleep(3.2)

        bad = sorted(at for at, code in results if code != 200)
        print(json.dumps({"requests": len(results), "app_down_s": [round(down_at, 2), round(up_at, 2)], "non_200": len(bad), "first_s": bad[0] if bad else None, "last_s": bad[-1] if bad else None}))
        for second in range(18):
            row = [code for at, code in results if second <= at < second + 1]
            print(f"{second:2d}s 요청 {len(row):2d} 비정상 {sum(code != 200 for code in row)}")
        logs = subprocess.run(["docker", "logs", CONTAINER], capture_output=True, text=True).stderr
        print("no live upstreams:", logs.count("no live upstreams"), "/ temporarily disabled:", logs.count("temporarily disabled"))
        addresses = subprocess.run(["docker", "exec", CONTAINER, "getent", "ahosts", "host.docker.internal"], capture_output=True, text=True).stdout
        print("host.docker.internal 주소 종류:", sorted({"IPv6" if ":" in line.split()[0] else "IPv4" for line in addresses.splitlines()}))
    finally:
        server.kill()
        subprocess.run(["docker", "rm", "-f", CONTAINER], capture_output=True)


if __name__ == "__main__":
    main()
