#!/usr/bin/env python3
"""Render the isolated development object store; credentials are required separately."""
import argparse
import json

from render import LABELS, database, resource

# The 4.52 release index, so each node runs its own platform's child: linux/amd64 a54ba35b (the earlier pin)
# or linux/arm64 18a805c8 (what the target node has run since it moved to arm64 on 2026-11-06).
IMAGE = "docker.io/chrislusf/seaweedfs:4.52@sha256:57877e32bb90073b37dc109b438ba66e84fd5ed9ced5f5c04f56444ef243ce59"
SELECTOR = {"app.kubernetes.io/name": "target-seaweedfs"}
# T3's namespace template and T9's rehearsal delivery use this label. NetworkPolicy cannot match a
# namespace-name prefix, so this label is what identifies a rehearsal namespace, not "rehearsal-*".
REHEARSAL_NAMESPACE = {"app.kubernetes.io/part-of": "bravo-rehearsal"}
REHEARSAL_CLIENTS = (
    # T15 verification Job (services/rehearsal-runner/src/rehearsal_runner/bucket_job.py).
    {"matchLabels": {"role": "bucket-verifier"}},
    # T3 isolation probe: its P4 control must reach 8333, or the gate stops before the Job runs.
    {"matchLabels": {"app.kubernetes.io/component": "probe"},
     "matchExpressions": [{"key": "bravo.dev/isolation-run", "operator": "Exists"}]},
)


def objects(node_name):
    # Use the database renderer's exact-node validation and retained local PV shape.
    volume = database(node_name)[2]
    volume["metadata"]["name"] = "bravo-target-dev-objects"
    volume["spec"]["local"]["path"] = "/var/lib/bravo-target-dev/objects"
    volume["spec"]["claimRef"] = {"namespace": "target-dev", "name": "target-objects"}
    claim = resource("v1", "PersistentVolumeClaim", "target-objects", spec={
        "accessModes": ["ReadWriteOnce"], "storageClassName": "bravo-target-local",
        "volumeName": "bravo-target-dev-objects",
        "resources": {"requests": {"storage": "8Gi"}},
    })
    service = resource("v1", "Service", "seaweedfs", spec={
        "selector": SELECTOR, "ports": [{"name": "s3", "port": 8333, "targetPort": "s3"}],
    })
    deploy = resource("apps/v1", "Deployment", "target-seaweedfs", spec={
        "replicas": 1, "strategy": {"type": "Recreate"},
        "selector": {"matchLabels": SELECTOR},
        "template": {"metadata": {"labels": {**LABELS, **SELECTOR}}, "spec": {
            "automountServiceAccountToken": False,
            "serviceAccountName": "target-seaweedfs",
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000,
                                "runAsGroup": 1000, "fsGroup": 1000,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "seaweedfs", "image": IMAGE,
                "command": ["/usr/bin/weed"],
                "args": ["-logtostderr=true", "server", "-dir=/data", "-master.dir=/data",
                         "-master.peers=none", "-master.volumeSizeLimitMB=128", "-volume.max=8",
                         "-ip=127.0.0.1", "-ip.bind=127.0.0.1", "-filer", "-s3",
                         "-s3.port=8333", "-s3.ip.bind=0.0.0.0",
                         "-s3.config=/etc/seaweedfs-auth/s3.json", "-s3.iam=false",
                         "-s3.port.iceberg=0", "-s3.port.lance=0"],
                "ports": [{"name": "s3", "containerPort": 8333}],
                "startupProbe": {"tcpSocket": {"port": "s3"}, "periodSeconds": 5,
                                 "failureThreshold": 120},
                "readinessProbe": {"tcpSocket": {"port": "s3"}, "periodSeconds": 5},
                "securityContext": {"allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": True,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                              "limits": {"cpu": "1", "memory": "768Mi"}},
                "volumeMounts": [{"name": "data", "mountPath": "/data"},
                                 {"name": "tmp", "mountPath": "/tmp"},
                                 {"name": "auth", "mountPath": "/etc/seaweedfs-auth",
                                  "readOnly": True}],
            }],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "target-objects"}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}},
                        {"name": "auth", "secret": {"secretName": "target-seaweedfs-auth",
                            "optional": False, "defaultMode": 0o440,
                            "items": [{"key": "s3.json", "path": "s3.json"}]}}],
        }},
    })
    policy = resource("networking.k8s.io/v1", "NetworkPolicy", "target-object-clients", spec={
        "podSelector": {"matchLabels": SELECTOR}, "policyTypes": ["Ingress", "Egress"],
        "ingress": [{"from": [{"podSelector": {"matchLabels": {"bravo-object-client": "true"}}}],
                     "ports": [{"protocol": "TCP", "port": 8333}]}],
        "egress": [],
    })
    rehearsal_policy = resource("networking.k8s.io/v1", "NetworkPolicy", "target-object-rehearsal-clients", spec={
        "podSelector": {"matchLabels": SELECTOR}, "policyTypes": ["Ingress"],
        # Each peer pairs both selectors: as separate peers they would mean "either", not "both".
        "ingress": [{"from": [{"namespaceSelector": {"matchLabels": REHEARSAL_NAMESPACE}, "podSelector": clients}
                              for clients in REHEARSAL_CLIENTS],
                     "ports": [{"protocol": "TCP", "port": 8333}]}],
    })
    return [volume, claim, service, deploy, policy, rehearsal_policy]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-name", required=True)
    args = parser.parse_args()
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": objects(args.node_name)}, indent=2))
