"""Local HTTPS query qualification; isolated Docker project and disposable CA.

Usage: .venv/bin/python testing/query_api_tls.py --image sha256:<local-image-id> --output /private/output
No cloud credentials or production data are needed. Docker and openssl are required.
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "services/console/src"))


def command(args, **kwargs):
    return subprocess.check_output(args, stderr=subprocess.PIPE, timeout=120, **kwargs)


def main():
    from bravo_console.observations import T37Client

    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    image = args.image
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        parser.error("an immutable local image ID is required")
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    inspected = json.loads(command(["docker", "image", "inspect", image]))[0]
    assert inspected["Config"]["User"] == "10001:10001"
    project = "bravo-query-tls-" + secrets.token_hex(4)
    results = {}
    with tempfile.TemporaryDirectory(prefix="bravo-query-tls-") as temp:
        certs = Path(temp)
        issue = REPO / "services/observability/deploy/certs/issue.sh"
        command(["bash", str(issue), "ca", str(certs / "ca"), "3"])
        for name, sans in [
            ("prometheus", "DNS:prometheus"),
            ("console-api", "DNS:console-api"),
            ("api-server", "DNS:api-server,DNS:localhost,IP:127.0.0.1"),
        ]:
            command(
                ["bash", str(issue), "leaf", str(certs / "ca"), name, sans, str(certs / name), "1"]
            )
            # Disposable test keys inside a private temp root; containers run nonroot.
            (certs / name).chmod(0o755)
            (certs / name / "tls.key").chmod(0o644)
        env = dict(
            os.environ,
            CERT_DIR=str(certs),
            API_TOKEN=secrets.token_urlsafe(36),
            QUERY_API_IMAGE=image,
        )
        compose = [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(REPO / "services/observability/testing/compose.yaml"),
            "-f",
            str(REPO / "services/observability/testing/query-api-tls.override.yaml"),
        ]
        try:
            command(compose + ["up", "-d", "--no-build", "api", "prometheus"], env=env)
            address = command(compose + ["port", "api", "8080"], env=env).decode().strip()
            assert address.startswith("127.0.0.1:")
            port = int(address.split(":")[-1])
            origin = "https://" + address
            ctx = ssl.create_default_context(cafile=str(certs / "ca/ca.crt"))
            ctx.minimum_version = ssl.TLSVersion.TLSv1_3
            deadline = time.monotonic() + 40
            with httpx.Client(base_url=origin, verify=ctx, trust_env=False, timeout=3) as client:
                while True:
                    try:
                        health = client.get("/healthz")
                        assert health.status_code == 200
                        break
                    except (httpx.HTTPError, AssertionError):
                        if time.monotonic() > deadline:
                            raise
                        time.sleep(0.3)
                results["https_health"] = True
                route = "/v1/observability/environments"
                assert client.get(route).status_code == 401
                assert (
                    client.get(route, headers={"Authorization": "Bearer invalid"}).status_code
                    == 401
                )
                results["missing_wrong_token_denied"] = True
                response = client.get(
                    route, headers={"Authorization": "Bearer " + env["API_TOKEN"]}
                )
                assert response.status_code == 200
                assert all(x["state"] == "UNOBSERVABLE" for x in response.json()["environments"])
                results["real_prometheus_mtls_empty_metrics_unknown"] = True
                for name in ("aws", "onprem"):
                    doc = client.get(
                        f"/v1/observability/environments/{name}/http",
                        headers={"Authorization": "Bearer " + env["API_TOKEN"]},
                    ).json()
                    assert doc["state"] == "UNOBSERVABLE" and doc["p95_seconds"] is None

            async def console():
                adapter = T37Client(origin, env["API_TOKEN"], tls=ctx)
                try:
                    for name in ("aws", "onprem"):
                        doc = await adapter.http(name)
                        assert doc["state"] == "UNOBSERVABLE" and doc["p95_seconds"] is None
                finally:
                    await adapter.aclose()

            asyncio.run(console())
            results["actual_console_adapter_https"] = True
            with socket.create_connection(("127.0.0.1", port), timeout=3) as raw:
                with ctx.wrap_socket(raw, server_hostname="localhost") as secured:
                    assert secured.version() == "TLSv1.3"
            results["tls13"] = True
            try:
                httpx.get("http://" + address + "/healthz", trust_env=False, timeout=3)
                raise AssertionError("plain_http_accepted")
            except httpx.RemoteProtocolError:
                results["plain_http_denied"] = True
            command(
                compose + ["exec", "-T", "api", "python", "-m", "bravo_observability.healthcheck"],
                env=env,
            )
            results["container_verified_healthcheck"] = True
            command(compose + ["stop", "prometheus"], env=env)
            with httpx.Client(base_url=origin, verify=ctx, trust_env=False, timeout=8) as client:
                response = client.get(
                    route, headers={"Authorization": "Bearer " + env["API_TOKEN"]}
                )
                assert (
                    response.status_code == 503 and response.json()["detail"] == "store_unavailable"
                )
            results["store_down_503"] = True
            (output / "qualification.json").write_text(
                json.dumps(
                    {
                        "runtime": "local-docker",
                        "image_id": image,
                        "results": results,
                        "operational_deployment": False,
                    },
                    indent=2,
                )
                + "\n"
            )
            print(json.dumps(results))
        finally:
            command(compose + ["down", "-v", "--remove-orphans"], env=env)
            remaining = (
                command(
                    [
                        "docker",
                        "ps",
                        "-aq",
                        "--filter",
                        "label=com.docker.compose.project=" + project,
                    ]
                )
                .decode()
                .strip()
            )
            assert not remaining
            (output / "cleanup.json").write_text(
                json.dumps({"containers_remaining": False, "temporary_keys_removed_on_exit": True})
            )
            print("owned containers removed")


if __name__ == "__main__":
    main()
