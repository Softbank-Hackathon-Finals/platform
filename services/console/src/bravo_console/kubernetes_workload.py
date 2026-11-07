"""GET-only Kubernetes observations. No policy verdict or operational effects.

Service selectors and container host references are configuration relationships,
not observed traffic. Only the exact registered /healthz probe proves an HTTP edge.
"""

import argparse
import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from .workload import identifier, safe_text

KINDS = {"namespace", "services", "pods", "networkpolicies"}
NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")


class CollectionError(Exception):
    pass


def opaque(*parts):
    return "r-" + hashlib.sha256(json.dumps(parts).encode()).hexdigest()[:24]


@dataclass(frozen=True)
class ClusterRegistration:
    environment_id: str
    lane: str
    cluster_uid: str
    namespace: str
    services: tuple

    @classmethod
    def parse(cls, value):
        if type(value) is not dict or set(value) != {
            "environment_id",
            "lane",
            "cluster_uid",
            "namespace",
            "services",
        }:
            raise ValueError("explicit_cluster_registration_required")
        identifier(value["environment_id"])
        if (
            value["lane"] not in {"source", "target"}
            or str(uuid.UUID(value["cluster_uid"])) != value["cluster_uid"]
        ):
            raise ValueError("registered_cluster_identity_required")
        if not isinstance(value["namespace"], str) or not NAME.fullmatch(value["namespace"]):
            raise ValueError("registered_namespace_required")
        services = value["services"]
        if type(services) is not list or not 1 <= len(services) <= 8:
            raise ValueError("bounded_registered_services_required")
        names = set()
        for entry in services:
            if type(entry) is not dict or set(entry) != {"name", "label", "kind", "probe_port"}:
                raise ValueError("registered_service_required")
            if not NAME.fullmatch(entry["name"]) or entry["name"] in names:
                raise ValueError("unique_registered_service_required")
            names.add(entry["name"])
            safe_text(entry["label"], 48)
            if entry["kind"] not in {
                "app",
                "database",
                "storage",
                "queue",
                "identity",
                "observability",
                "edge",
            }:
                raise ValueError("registered_service_kind_required")
            if entry["probe_port"] is not None and (
                type(entry["probe_port"]) is not int or not 1 <= entry["probe_port"] <= 65535
            ):
                raise ValueError("registered_probe_port_required")
        return cls(
            **(value | {"services": tuple(MappingProxyType(dict(item)) for item in services)})
        )

    @property
    def scope_digest(self):
        value = dict(
            environment_id=self.environment_id,
            lane=self.lane,
            cluster_uid=self.cluster_uid,
            namespace=self.namespace,
            services=[dict(item) for item in self.services],
        )
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def producer_digest():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class KubectlReader:
    """The host supplies an explicit, previously verified credential path/tool."""

    def __init__(self, prefix, *, env=None):
        if not prefix or any(type(arg) is not str or not arg for arg in prefix):
            raise ValueError("explicit_kubernetes_client_required")
        if any(
            arg.split("=", 1)[0]
            in {"--insecure-skip-tls-verify", "--token", "--password", "--log-file"}
            for arg in prefix
        ):
            raise CollectionError("VERIFIED_KUBERNETES_TLS_REQUIRED")
        for index, arg in enumerate(prefix):
            if arg == "--server" and (
                index + 1 == len(prefix) or not prefix[index + 1].startswith("https://")
            ):
                raise CollectionError("VERIFIED_KUBERNETES_TLS_REQUIRED")
            if arg.startswith("--server=") and not arg.startswith("--server=https://"):
                raise CollectionError("VERIFIED_KUBERNETES_TLS_REQUIRED")
        index = 1
        while index < len(prefix):
            option, separator, value = prefix[index].partition("=")
            if option not in {"--kubeconfig", "--context", "--server", "--tls-server-name"}:
                raise CollectionError("READ_SCOPE_INVALID")
            if separator:
                if not value:
                    raise CollectionError("READ_SCOPE_INVALID")
                index += 1
            else:
                if index + 1 == len(prefix) or prefix[index + 1].startswith("-"):
                    raise CollectionError("READ_SCOPE_INVALID")
                index += 2
        self.prefix = tuple(prefix)
        self.env = env
        value = self.run(["config", "view", "--minify", "-o", "json"])
        clusters = value.get("clusters", [])
        if (
            len(clusters) != 1
            or clusters[0].get("cluster", {}).get("insecure-skip-tls-verify")
            or not clusters[0].get("cluster", {}).get("server", "").startswith("https://")
        ):
            raise CollectionError("VERIFIED_KUBERNETES_TLS_REQUIRED")
        for user in value.get("users", []):
            plugin = user.get("user", {}).get("exec", {})
            if Path(plugin.get("command", "")).name not in {"aws", "aws.exe"}:
                continue
            args = plugin.get("args", [])
            if type(args) is not list or any(type(arg) is not str for arg in args):
                raise CollectionError("PERSONAL_AWS_PROFILE_REQUIRED")
            plugin_env = plugin.get("env")
            if plugin_env is None:
                plugin_env = []
            if (
                type(plugin_env) is not list
                or any(
                    type(item) is not dict
                    or set(item) != {"name", "value"}
                    or type(item["name"]) is not str
                    or not item["name"]
                    or type(item["value"]) is not str
                    for item in plugin_env
                )
                or len({item["name"] for item in plugin_env}) != len(plugin_env)
            ):
                raise CollectionError("PERSONAL_AWS_PROFILE_REQUIRED")
            if any(
                item.get("name") in {"AWS_PROFILE", "AWS_DEFAULT_PROFILE"}
                and item.get("value") != "bravo-personal"
                for item in plugin_env
            ):
                raise CollectionError("PERSONAL_AWS_PROFILE_REQUIRED")
            profiles = []
            for index, arg in enumerate(args):
                if arg == "--profile":
                    profiles.append(args[index + 1] if index + 1 < len(args) else None)
                elif arg.startswith("--profile="):
                    profiles.append(arg.split("=", 1)[1])
            if profiles and profiles != ["bravo-personal"]:
                raise CollectionError("PERSONAL_AWS_PROFILE_REQUIRED")
            effective = dict(os.environ if env is None else env)
            effective.update({item["name"]: item["value"] for item in plugin_env})
            inherited = [
                effective[key] for key in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE") if key in effective
            ]
            if not profiles and (not inherited or any(p != "bravo-personal" for p in inherited)):
                raise CollectionError("PERSONAL_AWS_PROFILE_REQUIRED")

    def run(self, args):
        # Fixed commands only; no shell, write verbs, log collection or exec.
        valid = args == ["config", "view", "--minify", "-o", "json"]
        if len(args) == 3 and args[:2] == ["get", "--raw"]:
            valid = bool(
                re.fullmatch(
                    r"/api/v1/namespaces/[a-z0-9.-]+/services/[a-z0-9.-]+:[0-9]{1,5}/proxy/healthz",
                    args[2],
                )
            )
        if len(args) >= 4 and args[0] == "get" and args[1] in KINDS and args[-2:] == ["-o", "json"]:
            if args[1] == "namespace":
                valid = args == ["get", "namespace", "kube-system", "-o", "json"]
            else:
                scope = args[2:-2]
                valid = (
                    len(scope) in (2, 3)
                    and scope[-2] == "--namespace"
                    and bool(NAME.fullmatch(scope[-1]))
                    and (len(scope) == 2 or bool(NAME.fullmatch(scope[0])))
                )
        if not valid:
            raise CollectionError("READ_SCOPE_INVALID")
        process = None
        try:
            process = subprocess.Popen(
                [*self.prefix, "--request-timeout=8s", *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.env,
                start_new_session=True,
            )
            output = bytearray()
            deadline = min(time.monotonic() + 15, getattr(self, "deadline", float("inf")))
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while True:
                    if time.monotonic() >= deadline:
                        raise CollectionError("KUBERNETES_READ_TIMEOUT")
                    if not selector.select(timeout=min(0.5, max(0, deadline - time.monotonic()))):
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    output.extend(chunk)
                    if len(output) > 2_000_000:
                        raise CollectionError("KUBERNETES_READ_LIMIT")
            process.wait(timeout=max(0.01, deadline - time.monotonic()))
            if process.returncode or not output:
                raise CollectionError("KUBERNETES_READ_UNCONFIRMED")
            return json.loads(output)
        except (OSError, subprocess.SubprocessError, ValueError):
            raise CollectionError("KUBERNETES_READ_UNCONFIRMED") from None
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=3)
                process.stdout.close()

    def get(self, kind, name=None, *, namespace=None):
        if kind not in KINDS or (name is not None and not NAME.fullmatch(name)):
            raise CollectionError("READ_SCOPE_INVALID")
        args = ["get", kind]
        if name:
            args.append(name)
        if namespace:
            if not NAME.fullmatch(namespace):
                raise CollectionError("READ_SCOPE_INVALID")
            args += ["--namespace", namespace]
        return self.run([*args, "-o", "json"])

    def health(self, registration, service):
        port = service["probe_port"]
        if port is None:
            return None
        # Literal path; no browser-supplied URL, token, user ID or query.
        path = f"/api/v1/namespaces/{registration.namespace}/services/{service['name']}:{port}/proxy/healthz"
        try:
            value = self.run(["get", "--raw", path])
            return type(value) is dict and value.get("ok") is True
        except CollectionError:
            return None


def collect(reader, registration, *, source_revision, now=None):
    r = registration
    if not isinstance(source_revision, str) or not re.fullmatch("[a-f0-9]{40}", source_revision):
        raise ValueError("qualified_source_revision_required")
    if isinstance(reader, KubectlReader):
        reader.deadline = time.monotonic() + 60
    started = now or datetime.now(UTC)
    if reader.get("namespace", "kube-system").get("metadata", {}).get("uid") != r.cluster_uid:
        raise CollectionError("CLUSTER_IDENTITY_MISMATCH")
    services = reader.get("services", namespace=r.namespace).get("items", [])
    pods = reader.get("pods", namespace=r.namespace).get("items", [])
    policies = reader.get("networkpolicies", namespace=r.namespace).get("items", [])
    if any(type(items) is not list or len(items) > 256 for items in (services, pods, policies)):
        raise CollectionError("RESOURCE_LIMIT_EXCEEDED")
    by_name = {item["metadata"]["name"]: item for item in services}
    if len(by_name) != len(services):
        raise CollectionError("RESOURCE_IDENTITY_AMBIGUOUS")
    nodes, edges, facets, probes, pod_objects = {}, [], {}, [], {}
    dns_nodes = {}
    for row, entry in enumerate(r.services):
        obj = by_name.get(entry["name"])
        if obj is None:
            continue
        meta = obj["metadata"]
        if (
            meta.get("namespace") != r.namespace
            or not meta.get("uid")
            or meta.get("deletionTimestamp")
        ):
            raise CollectionError("SERVICE_IDENTITY_UNCONFIRMED")
        sid = opaque(r.environment_id, "service", entry["name"])
        nodes[sid] = dict(
            id=sid,
            label=entry["label"],
            resource_uid=meta["uid"],
            detail="등록 Service · 업무 통신 별도 확인",
            kind=entry["kind"],
            lane=r.lane,
            column=0,
            row=row,
            health="unknown",
            security="unknown",
            health_evidence=None,
            security_evidence=None,
        )
        for suffix in (
            "",
            f".{r.namespace}",
            f".{r.namespace}.svc",
            f".{r.namespace}.svc.cluster.local",
        ):
            dns_nodes[entry["name"] + suffix] = sid
        selector = obj.get("spec", {}).get("selector", {})
        selected = [
            p
            for p in pods
            if selector
            and all(
                p.get("metadata", {}).get("labels", {}).get(k) == v for k, v in selector.items()
            )
        ]
        if len(selected) > 12:
            raise CollectionError("POD_LIMIT_EXCEEDED")
        facets[sid] = dict(
            ready=None, summary=f"Service 선택 Pod {len(selected)}개 · 통신 성공 미판정"
        )
        for index, pod in enumerate(sorted(selected, key=lambda p: p["metadata"]["name"])):
            metadata = pod["metadata"]
            if (
                metadata.get("namespace") != r.namespace
                or not metadata.get("uid")
                or metadata.get("deletionTimestamp")
            ):
                continue
            pid = opaque(r.environment_id, "pod", metadata["name"])
            statuses = pod.get("status", {}).get("containerStatuses", [])
            containers = pod.get("spec", {}).get("containers", [])
            if not 1 <= len(containers) <= 8:
                raise CollectionError("CONTAINER_LIMIT_EXCEEDED")
            conditions = [
                c for c in pod.get("status", {}).get("conditions", []) if c.get("type") == "Ready"
            ]
            state = conditions[0].get("status") if len(conditions) == 1 else None
            ready = True if state == "True" else False if state == "False" else None
            # Readiness is a facet; it never becomes HTTP health or a security verdict.
            nodes[pid] = dict(
                id=pid,
                label=f"{entry['label']} · Pod {index + 1}",
                resource_uid=metadata["uid"],
                detail=f"컨테이너 준비 {sum(c.get('ready') is True for c in statuses)}/{len(containers)} · Pod Ready 별도 관측 · 업무 응답 미측정",
                kind=entry["kind"],
                lane=r.lane,
                column=1,
                row=min(row + index, 9),
                health="unknown",
                security="unknown",
                health_evidence=None,
                security_evidence=None,
            )
            facets[pid] = dict(ready=ready, summary=nodes[pid]["detail"])
            pod_objects[pid] = pod
            edges.append(
                dict(
                    id=opaque(sid, pid),
                    source=sid,
                    target=pid,
                    label="Service selector",
                    kind="dependency",
                    resource_uid=opaque(meta["uid"], metadata["uid"]),
                    state="unknown",
                    evidence_id=None,
                )
            )
        if entry["probe_port"] is not None:
            probes.append((sid, reader.health(r, entry)))
    for pid, pod in pod_objects.items():
        status = {c["name"]: c for c in pod.get("status", {}).get("containerStatuses", [])}
        for index, container in enumerate(pod["spec"]["containers"]):
            cid = opaque(pid, container["name"])
            current = status.get(container["name"], {})
            # Bind a restarted container to its current runtime identity, never just its name.
            runtime_uid = current.get("containerID")
            uid = opaque(
                pod["metadata"]["uid"], runtime_uid or "unconfirmed", current.get("restartCount", 0)
            )
            ready = current.get("ready") if runtime_uid else None
            nodes[cid] = dict(
                id=cid,
                label=f"{nodes[pid]['label']} · C{index + 1}",
                resource_uid=uid,
                detail=f"컨테이너 준비 {ready is True} · 재시작 {current.get('restartCount', 0)}회 · 보안 미판정",
                kind=nodes[pid]["kind"],
                lane=r.lane,
                column=2,
                row=nodes[pid]["row"],
                health="unknown",
                security="unknown",
                health_evidence=None,
                security_evidence=None,
            )
            facets[cid] = dict(ready=ready, summary=nodes[cid]["detail"])
            edges.append(
                dict(
                    id=opaque(pid, cid),
                    source=pid,
                    target=cid,
                    label="Pod 소속",
                    kind="dependency",
                    resource_uid=opaque(pod["metadata"]["uid"], uid),
                    state="unknown",
                    evidence_id=None,
                )
            )
            for env in container.get("env", []):
                if (
                    env.get("name")
                    not in {"DB_HOST", "S3_ENDPOINT", "SQS_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT"}
                    or type(env.get("value")) is not str
                ):
                    continue
                value = env["value"]
                try:
                    hostname = urlsplit(value).hostname if "://" in value else value
                except ValueError:
                    continue
                target = dns_nodes.get(hostname)
                if target:
                    edges.append(
                        dict(
                            id=opaque(cid, target, "config"),
                            source=cid,
                            target=target,
                            label="컨테이너 설정 참조",
                            kind="dependency",
                            resource_uid=opaque(uid, nodes[target]["resource_uid"]),
                            state="unknown",
                            evidence_id=None,
                        )
                    )
    if not nodes or len(nodes) > 23 or len(edges) > 40:
        raise CollectionError("TOPOLOGY_UNAVAILABLE_OR_OVERSIZED")
    check = {
        obj["metadata"]["name"]: obj
        for obj in reader.get("services", namespace=r.namespace).get("items", [])
    }
    for entry in r.services:
        before, current = by_name.get(entry["name"]), check.get(entry["name"])
        if (before is None) != (current is None) or (
            before
            and (
                before["metadata"].get("uid") != current["metadata"].get("uid")
                or before.get("spec") != current.get("spec")
            )
        ):
            raise CollectionError("SERVICE_CHANGED_DURING_READ")
    after = reader.get("namespace", "kube-system")
    if after.get("metadata", {}).get("uid") != r.cluster_uid:
        raise CollectionError("CLUSTER_IDENTITY_CHANGED")
    finished = now or datetime.now(UTC)
    if finished - started > timedelta(seconds=90):
        raise CollectionError("OBSERVATION_TOO_SLOW")
    # No raw pod spec, environment, IP, image path, Secret or command leaves this collector.
    return dict(
        producer_revision=source_revision,
        producer_module_sha256=producer_digest(),
        registration_digest=r.scope_digest,
        cluster_uid=r.cluster_uid,
        environment_id=r.environment_id,
        lane=r.lane,
        observed_at=started.isoformat(),
        expires_at=(started + timedelta(seconds=90)).isoformat(),
        cluster_identity_matches=True,
        namespace_policy_count=len(policies),
        registered_service_count=len(r.services),
        observed_service_count=sum(s["name"] in by_name for s in r.services),
        nodes=list(nodes.values()),
        edges=edges,
        facets=facets,
        probes=probes,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registration", required=True)
    args = parser.parse_args()
    try:
        config = json.loads(open(args.registration).read())
        producer = config["producer"]
        if (
            set(producer) != {"revision", "sha256"}
            or not re.fullmatch("[a-f0-9]{40}", producer["revision"])
            or producer["sha256"] != producer_digest()
        ):
            raise ValueError("fixed_producer_artifact_required")
        r = ClusterRegistration.parse(config["registration"])
        prefix = config.get("kubectl")
        if prefix is None:
            kubeconfig = os.environ.get("KUBECONFIG")
            if not kubeconfig:
                raise ValueError("explicit_kubeconfig_required")
            prefix = ["kubectl", "--kubeconfig", kubeconfig]
        env = {key: value for key, value in os.environ.items() if not key.startswith("AWS_")}
        env.update(
            AWS_PROFILE="bravo-personal",
            AWS_REGION="ap-northeast-2",
            AWS_EC2_METADATA_DISABLED="true",
            AWS_IGNORE_CONFIGURED_ENDPOINT_URLS="true",
            AWS_PAGER="",
        )
        result = collect(KubectlReader(prefix, env=env), r, source_revision=producer["revision"])
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    except Exception:
        print('{"error":"COLLECTION_UNCONFIRMED"}')
        sys.exit(3)


if __name__ == "__main__":
    main()
