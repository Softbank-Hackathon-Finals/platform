"""Render the Product Reader config v2 ConfigMap from the running v1 config and the cluster JWKS.

Since #503 the Reader accepts only config v2 (`job_identity`, platform Job token) and refuses v1
(`github_public_jwks_file`, `sts_provider`); the v1 Reader refuses v2. The image pin (#540 and
later) and this ConfigMap must therefore switch in the same rollout: create this ConfigMap first
(unused), then point `productReader.runtime.configuration` at it when the new image syncs.

    python3 operations/product-reader/reader_config_v2.py --v1 reader.json --jwks jwks.json \
        --issuer https://kubernetes.default.svc.cluster.local > configmap.json

Prints the ConfigMap (content-addressed name) on stdout and its sha256 on stderr. No secrets.
"""

import argparse
import hashlib
import json
import sys

NAMESPACE = "platform-state"
JOB_SUBJECT = "system:serviceaccount:bravo-initial:initial-proposal"  # initial_proposal_job NAMESPACE/SA
AUDIENCE = "bravo-subscription-broker"
JWKS_PATH = "/product-read/cluster-jwks.json"
KEPT = ("tenant_id", "catalog_root", "catalog_key_file")


def config_sha256(data):
    """Exactly infra/gitops/bootstrap.py's adoption check (compact separators), not a display hash."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def render(v1, jwks, issuer):
    if type(v1) is not dict or v1.get("version") != 1 or any(k not in v1 for k in KEPT):
        raise ValueError("reader_config_v1_required")
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if (not isinstance(keys, list) or not 1 <= len(keys) <= 32
            or any(set(k) & {"d", "p", "q", "dp", "dq", "qi"} or k.get("kty") != "RSA" for k in keys)):
        raise ValueError("public_rsa_jwks_required")
    if not issuer.startswith("https://"):
        raise ValueError("issuer_required")
    config = {"version": 2, **{k: v1[k] for k in KEPT},
              "job_identity": {"issuer": issuer, "audience": AUDIENCE, "subject": JOB_SUBJECT,
                               "jwks_file": JWKS_PATH}}
    data = {"reader.json": json.dumps(config, sort_keys=True, indent=1) + "\n",
            "cluster-jwks.json": json.dumps({"keys": keys}, sort_keys=True) + "\n"}
    digest = config_sha256(data)
    return {"apiVersion": "v1", "kind": "ConfigMap",
            "metadata": {"name": f"t10-product-reader-config-v2-{digest[:16]}", "namespace": NAMESPACE,
                         "labels": {"component": "platform", "app.kubernetes.io/name": "t10-product-reader"}},
            "data": data}, digest


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--v1", required=True)
    p.add_argument("--jwks", required=True)
    p.add_argument("--issuer", required=True)
    a = p.parse_args(argv)
    manifest, digest = render(json.load(open(a.v1)), json.load(open(a.jwks)), a.issuer)
    json.dump(manifest, sys.stdout, sort_keys=True)
    print(digest, file=sys.stderr)


def _check():
    v1 = {"version": 1, "tenant_id": "t", "catalog_root": "/c", "catalog_key_file": "/k/key",
          "github_public_jwks_file": "/g", "sts_provider": {}}
    jwks = {"keys": [{"kty": "RSA", "kid": "a", "n": "x", "e": "AQAB"}]}
    cm, digest = render(v1, jwks, "https://kubernetes.default.svc.cluster.local")
    from pathlib import Path
    boot = Path(__file__).resolve().parents[2] / "infra/gitops/bootstrap.py"
    assert "separators=(\",\", \":\")" in boot.read_text()  # same serialisation as the adoption check
    assert digest == hashlib.sha256(json.dumps(cm["data"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    cfg = json.loads(cm["data"]["reader.json"])
    assert cfg["version"] == 2 and "github_public_jwks_file" not in cfg and cfg["job_identity"]["subject"] == JOB_SUBJECT
    assert set(cfg) == {"version", "tenant_id", "catalog_root", "catalog_key_file", "job_identity"}  # Reader _FIELDS
    for bad in ({**v1, "version": 2}, {"version": 1}):
        try:
            render(bad, jwks, "https://x")
            raise AssertionError("accepted")
        except ValueError:
            pass
    try:
        render(v1, {"keys": [{"kty": "RSA", "d": "secret"}]}, "https://x")
        raise AssertionError("private key accepted")
    except ValueError:
        pass


if __name__ == "__main__":
    if sys.argv[1:] == ["--self-check"]:
        _check()
        print("ok")
    else:
        main()
