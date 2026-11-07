"""Trusted GET observations for the existing renderer and optional native T10 port.

The standalone observation document contains no operational plan or approvals.
Native PlanWorkload assembly still requires current T10 identity and permissions.
"""

import asyncio
import hashlib
import json
import threading
import uuid
from datetime import UTC, datetime, timedelta

from .kubernetes_workload import ClusterRegistration, opaque
from .workload import PlanWorkload, RegisteredWorkload, identifier, safe_text, validate_projection


class ObservedWorkloads:
    def __init__(self, *, application_id, source_revision, registrations, collect, clock=None):
        identifier(application_id)
        if (
            not isinstance(source_revision, str)
            or len(source_revision) != 40
            or any(c not in "0123456789abcdef" for c in source_revision)
            or not 1 <= len(registrations) <= 2
            or not callable(collect)
            or any(type(r) is not ClusterRegistration for r in registrations)
            or len({r.environment_id for r in registrations}) != len(registrations)
            or len({r.lane for r in registrations}) != len(registrations)
        ):
            raise ValueError("fixed_observation_registrations_required")
        self.application_id, self.source_revision = application_id, source_revision
        self.registrations = tuple(registrations)
        self.collect = collect
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lock = threading.RLock()
        self.snapshots, self.failures, self.issued = {}, set(), {}
        self.stream_id = "observation-" + uuid.uuid4().hex
        self.revision = 1

    def refresh(self, registration):
        try:
            value = self.collect(registration)
            if (
                value["environment_id"] != registration.environment_id
                or value["lane"] != registration.lane
                or value["cluster_identity_matches"] is not True
            ):
                raise ValueError("registered_observation_mismatch")
            if (
                any(
                    n.get("health") != "unknown"
                    or n.get("security") != "unknown"
                    or n.get("health_evidence") is not None
                    or n.get("security_evidence") is not None
                    or n.get("lane") != registration.lane
                    for n in value["nodes"]
                )
                or any(
                    e.get("state") != "unknown" or e.get("evidence_id") is not None
                    for e in value["edges"]
                )
                or any(type(ok) is not bool and ok is not None for _, ok in value["probes"])
                or len({node for node, _ in value["probes"]}) != len(value["probes"])
            ):
                raise ValueError("unsupported_observation_claim")
            # Validate the display shape through the existing contract, without
            # inventing or registering an operational T10 plan.
            trial = self._graph([value], now=self.clock())
            self._validate_graph(trial)
            with self.lock:
                self.snapshots[registration.environment_id] = json.loads(
                    json.dumps(value, allow_nan=False)
                )
                self.failures.discard(registration.environment_id)
                self.revision += 1
                self.issued.clear()
        except Exception:
            with self.lock:
                self.failures.add(registration.environment_id)
                self.revision += 1
                self.issued.clear()

    async def poll(self):
        while True:
            await asyncio.gather(*(asyncio.to_thread(self.refresh, r) for r in self.registrations))
            await asyncio.sleep(20)

    def _graph(self, snapshots, *, now):
        nodes, edges, facets, evidence = [], [], {}, []
        collected = []
        for item in snapshots:
            observed = datetime.fromisoformat(item["observed_at"])
            expires = datetime.fromisoformat(item["expires_at"])
            if (
                observed.tzinfo is None
                or expires.tzinfo is None
                or not 0 < (expires - observed).total_seconds() <= 90
                or observed > now
            ):
                raise ValueError("invalid_observation_time")
            fresh = now < expires and item["environment_id"] not in self.failures
            collected.append(observed)
            batch = json.loads(json.dumps(item["nodes"]))
            nodes.extend(batch)
            edges.extend(json.loads(json.dumps(item["edges"])))
            for key, facet in item["facets"].items():
                if set(facet) != {"ready", "summary"} or (
                    facet["ready"] is not None and type(facet["ready"]) is not bool
                ):
                    raise ValueError("invalid_readiness_facet")
                safe_text(facet["summary"], 240)
                facets[key] = (
                    dict(facet)
                    if fresh
                    else dict(ready=None, summary="관측 만료·수집 실패 · 현재 준비 상태 미확인")
                )
            service_nodes = {n["id"]: n for n in batch}
            if fresh:
                for resource_id, ok in item["probes"]:
                    if ok is not True:
                        continue
                    node = service_nodes[resource_id]
                    eid = opaque(resource_id, node["resource_uid"], "health")
                    node["health"] = "ready"
                    node["health_evidence"] = eid
                    evidence.append(
                        dict(
                            id=eid,
                            resource_id=resource_id,
                            resource_uid=node["resource_uid"],
                            kind="health",
                            outcome="verified",
                            summary="Kubernetes API 프록시 /healthz ok=true. DB·업무·사용자 경로는 별도 검증.",
                            observed_at=item["observed_at"],
                            expires_at=item["expires_at"],
                        )
                    )
                    collector_id = opaque(item["environment_id"], "http-reader")
                    collector_uid = opaque(self.stream_id, item["environment_id"])
                    if not any(n["id"] == collector_id for n in nodes):
                        nodes.append(
                            dict(
                                id=collector_id,
                                label="관리 HTTP 탐침",
                                resource_uid=collector_uid,
                                detail="읽기 전용 API 프록시 탐침 · 사용자 트래픽이 아님",
                                kind="observability",
                                lane="control",
                                column=2,
                                row=0 if item["lane"] == "source" else 1,
                                health="unknown",
                                security="unknown",
                                health_evidence=None,
                                security_evidence=None,
                            )
                        )
                        facets[collector_id] = dict(
                            ready=None, summary="등록된 Service /healthz만 GET · 변경 권한 없음"
                        )
                    link_id = opaque(collector_id, resource_id)
                    link_uid = opaque(collector_uid, node["resource_uid"])
                    proof_id = opaque(link_id, "health")
                    edges.append(
                        dict(
                            id=link_id,
                            source=collector_id,
                            target=resource_id,
                            label="API 프록시 /healthz GET",
                            kind="http",
                            resource_uid=link_uid,
                            state="observed",
                            evidence_id=proof_id,
                        )
                    )
                    evidence.append(
                        dict(
                            id=proof_id,
                            resource_id=link_id,
                            resource_uid=link_uid,
                            kind="health",
                            outcome="verified",
                            summary="현재 Service UID에 결속한 API 프록시 HTTP 응답. 업무·DB 연결·TLS 동등성 미검증.",
                            observed_at=item["observed_at"],
                            expires_at=item["expires_at"],
                        )
                    )
        collected_at = max(collected) if collected else now
        return dict(
            nodes=nodes,
            edges=edges,
            evidence=evidence,
            responses=[],
            events=[],
            facets=facets,
            observed_at=collected_at.isoformat(),
            expires_at=(collected_at + timedelta(seconds=90)).isoformat(),
        )

    def _validate_graph(self, graph):
        # This is only an internal shape validator. No plan is issued/stored,
        # and the standalone HTTP document does not expose these test bindings.
        check = {k: v for k, v in graph.items() if k != "facets"}
        check.update(
            kind="console-workload.v1",
            authorizes_execution=False,
            plan_id="observation-shape-check",
            plan_digest="0" * 64,
            plan_version=1,
            revision=1,
            stream_id=self.stream_id,
            provenance="live",
            source_label="Kubernetes GET 관측",
        )
        validate_projection(
            check,
            {"plan_id": "observation-shape-check", "plan_digest": "0" * 64, "version": 1},
            self.clock(),
        )
        if set(graph["facets"]) != {n["id"] for n in graph["nodes"]}:
            raise ValueError("readiness_resource_mismatch")

    def read(self):
        now = self.clock()
        with self.lock:
            snapshots = list(self.snapshots.values())
            environments = []
            for r in self.registrations:
                item = self.snapshots.get(r.environment_id)
                current = (
                    item is not None
                    and now < datetime.fromisoformat(item["expires_at"])
                    and r.environment_id not in self.failures
                )
                environments.append(
                    dict(
                        environment_id=r.environment_id,
                        lane=r.lane,
                        status="CURRENT" if current else "STALE" if item else "UNAVAILABLE",
                        observed_at=item["observed_at"] if item else None,
                        namespace_policy_count=item["namespace_policy_count"] if current else None,
                        registered_service_count=len(r.services),
                        observed_service_count=item["observed_service_count"] if current else None,
                    )
                )
            graph = self._graph(snapshots, now=now) if snapshots else None
            if graph:
                self._validate_graph(graph)
            return dict(
                kind="console-observed-topology.v1",
                authorizes_execution=False,
                plan_connected=False,
                application_id=self.application_id,
                source_revision=self.source_revision,
                stream_id=self.stream_id,
                revision=self.revision,
                queried_at=now.isoformat(),
                environments=environments,
                graph=graph,
            )

    def load(self, scope, plan):
        with self.lock:
            if plan["workload_id"] != self.application_id or any(
                r.environment_id != plan[r.lane + "_environment_id"] for r in self.registrations
            ):
                raise ValueError("observation_plan_mismatch")
            doc = self.read()
            if doc["graph"] is None:
                raise ValueError("observation_unavailable")
            graph = {k: v for k, v in doc["graph"].items() if k != "facets"}
            graph.update(
                kind="console-workload.v1",
                authorizes_execution=False,
                plan_id=plan["plan_id"],
                plan_digest=plan["plan_digest"],
                plan_version=plan["version"],
                revision=self.revision,
                stream_id=self.stream_id,
                provenance="live",
                source_label="Kubernetes GET·명시적 HTTP 탐침",
            )
            raw = json.dumps(graph, sort_keys=True, separators=(",", ":")).encode()
            item = RegisteredWorkload(
                "registered-kubernetes", self.source_revision, hashlib.sha256(raw).hexdigest(), raw
            )
            key = (plan["plan_id"], plan["plan_digest"], plan["version"])
            if len(self.issued) >= 128:
                self.issued.clear()
            self.issued[key] = item
            return item

    def authenticate(self, scope, plan, item):
        with self.lock:
            return self.issued.get((plan["plan_id"], plan["plan_digest"], plan["version"])) is item

    def attach(self, api, authorize):
        return PlanWorkload(
            api,
            load=self.load,
            authenticate=self.authenticate,
            authorize=authorize,
            producer_id="registered-kubernetes",
            source_revision=self.source_revision,
            provenance="live",
            clock=self.clock,
        )
