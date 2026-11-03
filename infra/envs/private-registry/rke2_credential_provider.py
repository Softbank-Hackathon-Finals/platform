"""Run on the verified RKE2 host via SSM; private JSON configuration on stdin.

Install an upstream, hash-pinned ECR kubelet credential provider. No registry
password is printed, written, passed in argv, or stored in Kubernetes.
"""

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import time
import urllib.request

BINARY_URL = "https://s3.us-west-2.amazonaws.com/amazon-eks/1.35.9/2026-10-29/bin/linux/amd64/ecr-credential-provider"
BINARY_SHA = "9589cdea0d1ff3efd4d65d48eaed0b0ca6853f92334b3cd0f2d368b670e303b4"
BASE = pathlib.Path("/var/lib/rancher/credentialprovider")
CONFIG = BASE / "config.yaml"
BINARY = BASE / "bin/ecr-credential-provider"
DROPIN = pathlib.Path("/etc/rancher/rke2/config.yaml.d/90-bravo-ecr-credential-provider.yaml")
MAIN_CONFIG = pathlib.Path("/etc/rancher/rke2/config.yaml")
KUBE = ["/var/lib/rancher/rke2/bin/kubectl", "--kubeconfig", "/etc/rancher/rke2/rke2.yaml"]


class Failure(RuntimeError):
    pass


def run(command, phase, *, input_text=None, timeout=45):
    result = subprocess.run(command, check=False, input=input_text, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        raise Failure(phase + "_failed")
    return result.stdout


def provider_configuration(repository):
    if not re.fullmatch(r"[0-9]{12}\.dkr\.ecr\.ap-northeast-2\.amazonaws\.com/bravo/demo-chat", repository):
        raise Failure("repository_boundary_invalid")
    return {
        "apiVersion": "kubelet.config.k8s.io/v1", "kind": "CredentialProviderConfig",
        "providers": [{"name": "ecr-credential-provider", "matchImages": [repository],
                       "defaultCacheDuration": "0s", "apiVersion": "credentialprovider.kubelet.k8s.io/v1",
                       "env": [{"name": "AWS_REGION", "value": "ap-northeast-2"}]}],
    }


def pods():
    return json.loads(run(KUBE + ["get", "pods", "-A", "-o", "json"], "pod_inventory"))["items"]


def ready_counts():
    run(KUBE + ["get", "--raw=/readyz"], "api_readiness")
    nodes = json.loads(run(KUBE + ["get", "nodes", "-o", "json"], "node_inventory"))["items"]
    ready_nodes = sum(any(c.get("type") == "Ready" and c.get("status") == "True"
                          for c in n.get("status", {}).get("conditions", [])) for n in nodes)
    system = [p for p in pods() if p["metadata"]["namespace"] == "kube-system"]
    ready_system = sum(p.get("status", {}).get("phase") == "Succeeded" or (
        p.get("status", {}).get("phase") == "Running" and any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in p.get("status", {}).get("conditions", []))) for p in system)
    if len(nodes) != 1 or ready_nodes != 1 or len(system) < 17 or ready_system != len(system):
        raise Failure("cluster_not_ready")
    return len(nodes), ready_nodes, len(system), ready_system


def wait_ready():
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        try:
            return ready_counts()
        except (Failure, subprocess.TimeoutExpired, ValueError):
            time.sleep(3)
    raise Failure("cluster_readiness_timeout")


def kubelet_provider_flags():
    for process in pathlib.Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            args = (process / "cmdline").read_bytes().decode().split("\0")
        except (OSError, UnicodeError):
            continue
        if args and pathlib.Path(args[0]).name == "kubelet":
            return all(value in args for value in (
                "--image-credential-provider-bin-dir=" + str(BINARY.parent),
                "--image-credential-provider-config=" + str(CONFIG),
            ))
    return False


def ensure_owned(path, expected, mode, created):
    if path.exists():
        if path.is_symlink() or path.read_bytes() != expected or path.stat().st_uid != 0 or path.stat().st_mode & 0o777 != mode:
            raise Failure("existing_configuration_conflict")
        return False
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with path.open("xb") as stream:
        stream.write(expected)
    path.chmod(mode)
    created.append(path)
    return True


def main():
    os.umask(0o077)
    config = json.load(sys.stdin)
    repository = config["repository_url"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", config.get("digest", "")):
        raise Failure("image_digest_invalid")
    desired = (json.dumps(provider_configuration(repository), indent=2) + "\n").encode()
    if os.geteuid() != 0 or os.uname().machine != "x86_64":
        raise Failure("host_platform_invalid")
    if any(p["metadata"]["namespace"] != "kube-system" for p in pods()):
        raise Failure("active_workload_requires_owner_coordination")
    ready_counts()
    if any(os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_PROFILE", "AWS_WEB_IDENTITY_TOKEN_FILE")):
        raise Failure("unexpected_host_credential_environment")
    if pathlib.Path("/root/.aws/credentials").exists():
        raise Failure("unexpected_shared_host_credentials")
    original = MAIN_CONFIG.read_bytes()
    backup = pathlib.Path("/var/lib/bravo/private-registry/config.yaml.before")
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not backup.exists():
        backup.write_bytes(original)
        backup.chmod(0o600)
    dropin = (f"image-credential-provider-bin-dir: {BINARY.parent}\n"
              f"image-credential-provider-config: {CONFIG}\n").encode()
    # Check all existing targets before modifying anything.
    for path, expected in ((CONFIG, desired), (DROPIN, dropin)):
        if path.exists() and (path.is_symlink() or path.read_bytes() != expected or path.stat().st_uid != 0):
            raise Failure("existing_configuration_conflict")
    if BINARY.exists() and (BINARY.is_symlink() or hashlib.sha256(BINARY.read_bytes()).hexdigest() != BINARY_SHA):
        raise Failure("existing_binary_conflict")
    if BINARY.exists():
        binary = BINARY.read_bytes()
    else:
        with urllib.request.urlopen(BINARY_URL, timeout=90) as response:
            binary = response.read()
    if hashlib.sha256(binary).hexdigest() != BINARY_SHA:
        raise Failure("upstream_binary_checksum_mismatch")
    created = []
    changed = False
    try:
        changed |= ensure_owned(BINARY, binary, 0o755, created)
        changed |= ensure_owned(CONFIG, desired, 0o600, created)
        changed |= ensure_owned(DROPIN, dropin, 0o600, created)
        # The provider returns a password to kubelet through stdout. Inspect its
        # shape in memory only; never print or persist the response.
        request = {"apiVersion": "credentialprovider.kubelet.k8s.io/v1", "kind": "CredentialProviderRequest",
                   "image": repository + "@" + config["digest"]}
        result = subprocess.run([str(BINARY)], check=False, input=json.dumps(request), text=True, capture_output=True,
                                timeout=45, env={**os.environ, "AWS_REGION": "ap-northeast-2"})
        if result.returncode:
            raise Failure("instance_role_provider_authentication_failed")
        credentials = json.loads(result.stdout)
        auth = credentials.get("auth", {})
        if credentials.get("kind") != "CredentialProviderResponse" or not auth or not all(
            x.get("username") == "AWS" and bool(x.get("password")) for x in auth.values()
        ):
            raise Failure("provider_response_invalid")
        duration = credentials.get("cacheDuration")
        cache_key = credentials.get("cacheKeyType")
        if not isinstance(duration, str) or not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?(?:h|ms|m|us|ns|s))+", duration) or cache_key != "Registry":
            raise Failure("provider_cache_metadata_invalid")
        del credentials, auth, result
        if changed:
            run(["systemctl", "restart", "rke2-server"], "rke2_restart", timeout=180)
        counts = wait_ready()
        if not kubelet_provider_flags():
            raise Failure("kubelet_provider_flags_missing")
        if MAIN_CONFIG.read_bytes() != original:
            raise Failure("original_configuration_changed")
    except Exception:
        # Only paths newly created by this invocation are removed. Never change
        # a pre-existing provider or the foundation's primary configuration.
        for path in reversed(created):
            path.unlink()
        if changed:
            run(["systemctl", "restart", "rke2-server"], "rollback_restart", timeout=180)
            wait_ready()
        raise
    print(json.dumps({"provider_installed": True, "provider_binary_sha256": BINARY_SHA,
                      "provider_authentication_from_host_role_verified": True,
                      "provider_response_cache_duration": duration, "provider_response_cache_key_type": cache_key,
                      "kubelet_provider_flags_verified": True,
                      "original_rke2_config_unchanged": True, "rke2_service_restarted": changed,
                      "node_count": counts[0], "ready_node_count": counts[1],
                      "system_pod_count": counts[2], "ready_system_pod_count": counts[3],
                      "long_lived_registry_credentials_written": False,
                      "kubernetes_pull_secret_created": False, "cluster_image_pull_verified": False,
                      "credential_renewal_after_expiry_observed": False}))


if __name__ == "__main__":
    try:
        main()
    except Failure as error:
        print(json.dumps({"installation": "NOT_VERIFIED", "error_code": str(error)}))
        raise SystemExit(1) from None
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired):
        # Do not emit raw exceptions: provider responses/configuration are private.
        print(json.dumps({"installation": "NOT_VERIFIED", "error_code": "provider_installation_failed"}))
        raise SystemExit(1) from None
