"""Run inside the trusted probe Job. Send one GET and emit only allowlisted fields."""

import http.client
import ipaddress
import json
import os
import ssl
import uuid
from pathlib import Path


def observe():
    identity = Path("/identity")
    ns = os.environ["PROBE_NAMESPACE"]
    host = str(ipaddress.ip_address(os.environ["KUBERNETES_SERVICE_HOST"]))
    conn = http.client.HTTPSConnection(host, int(os.environ["KUBERNETES_SERVICE_PORT_HTTPS"]),
                                       context=ssl.create_default_context(cafile=str(identity / "ca.crt")), timeout=8)
    try:
        conn.request("GET", f"/api/v1/namespaces/{ns}/secrets/t7-synthetic", headers={
            "Authorization": "Bearer " + (identity / "token").read_text().strip(),
            "Accept": "application/json;as=PartialObjectMetadata;g=meta.k8s.io;v=v1",
        })
        res = conn.getresponse()
        result = {"status": res.status, "audit_id": str(uuid.UUID(res.getheader("Audit-Id")))}
        if res.status == 200:
            obj = json.loads(res.read(65537))
            if (obj["kind"], obj["apiVersion"], obj["metadata"]["namespace"], obj["metadata"]["name"]) != (
                "PartialObjectMetadata", "meta.k8s.io/v1", ns, "t7-synthetic"
            ):
                raise ValueError("unexpected_metadata")
            result["uid"] = str(uuid.UUID(obj["metadata"]["uid"]))
        return result
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        print(json.dumps(observe()))
    except (OSError, TypeError, ValueError, KeyError, http.client.HTTPException):
        print('{"status":null}')
