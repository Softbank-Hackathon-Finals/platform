"""Pin the registered base image the platform builds candidates on (`propose.build.base_file`).

Run once at registration by the operator with their own AWS identity: reads the base index from the private
ECR repository, each image platform's manifest and config, checks that every image was built from the
registered source revision, and prints the base file (no secret; ECR auth goes over a pipe, never argv).
The workflow host then needs no blob storage access: it reads manifests and pushes, nothing else.

    python3 infra/envs/candidate-registry/pin_base.py --repository "$REPOSITORY_URL" \
      --digest sha256:... --source-sha <40 hex> > candidate-base.json
"""

import argparse
import base64
import hashlib
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request

ACCEPT = ("application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, "
          "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def fetch(url, auth):
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers={"Authorization": auth, "Accept": ACCEPT}),
                         timeout=60) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        if error.code in (301, 302, 307) and error.headers.get("Location"):
            with urllib.request.urlopen(error.headers["Location"], timeout=60) as response:  # signed: no token
                return response.read()
        raise


def pin(repository, index_digest, source_sha, *, workdir="/app", copied=("app/",)):
    host, _, name = repository.partition("/")
    region = re.fullmatch(r"[0-9]{12}\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com", host)
    if region is None or not re.fullmatch(r"[a-f0-9]{40}", source_sha):
        raise ValueError("ecr_repository_and_source_sha_required")
    token = subprocess.run(["aws", "ecr", "get-login-password", "--region", region[1]], capture_output=True,
                           text=True, check=True).stdout.strip()
    auth = "Basic " + base64.b64encode(f"AWS:{token}".encode()).decode()
    base = f"https://{host}/v2/{name}"
    index_raw = fetch(f"{base}/manifests/{index_digest}", auth)
    if sha(index_raw) != index_digest:
        raise ValueError("index_digest_mismatch")
    platforms = {}
    for entry in json.loads(index_raw)["manifests"]:
        platform = entry.get("platform") or {}
        if platform.get("os") in (None, "unknown"):
            continue  # attestations
        manifest_raw = fetch(f"{base}/manifests/{entry['digest']}", auth)
        config_digest = json.loads(manifest_raw)["config"]["digest"]
        config = fetch(f"{base}/blobs/{config_digest}", auth)
        labels = (json.loads(config).get("config") or {}).get("Labels") or {}
        if sha(manifest_raw) != entry["digest"] or sha(config) != config_digest:
            raise ValueError("registry_content_mismatch")
        if labels.get("org.opencontainers.image.revision", source_sha) != source_sha:
            raise ValueError("base_built_from_another_revision")
        key = f"{platform['os']}/{platform['architecture']}" + (f"/{platform['variant']}" if platform.get("variant") else "")
        platforms[key] = {"manifest_digest": entry["digest"], "config": base64.b64encode(config).decode()}
    if not platforms:
        raise ValueError("no_image_platforms")
    return {"repository": repository, "source_sha": source_sha, "index_digest": index_digest, "workdir": workdir,
            "copied": list(copied), "platforms": platforms}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--source-sha", required=True)
    args = parser.parse_args()
    json.dump(pin(args.repository, args.digest, args.source_sha), sys.stdout, sort_keys=True)
