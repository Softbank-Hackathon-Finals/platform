#!/usr/bin/env python3
"""Pre-cutover target pooler: verified source egress plus the reviewed T12 contract."""
import argparse
import ipaddress
import json
import uuid
from pathlib import Path

from render import LABELS, resource

POOLER_IMAGE = 'ghcr.io/cloudnative-pg/pgbouncer:1.25.1@sha256:e6ddfe22d845e603825e235dd8334b21ecd125abea2a2172478f556b8dee2bb8'
SOURCE_HOST = 'source-db-rw.source-dev.svc.cluster.local'


def pooler(egress_address, egress_service_uid):
    address = ipaddress.IPv4Address(egress_address)
    if not address.is_private or any((address.is_loopback, address.is_link_local,
                                     address.is_multicast, address.is_unspecified)):
        raise ValueError('verified private source-for-pooler Service address required')
    if str(uuid.UUID(egress_service_uid)) != egress_service_uid:
        raise ValueError('exact egress Service UID required')
    name = 'target-pgbouncer'
    selector = {'app': name}
    labels = {**LABELS, **selector}
    security = {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                'capabilities': {'drop': ['ALL']}}
    config = resource('v1', 'ConfigMap', name + '-config', data={
        'pgbouncer.ini': Path(__file__).with_name('pgbouncer.ini').read_text()})
    pod = {
        'automountServiceAccountToken': False,
        'serviceAccountName': name,
        'securityContext': {'runAsNonRoot': True, 'runAsUser': 998, 'runAsGroup': 996,
                            'fsGroup': 996, 'seccompProfile': {'type': 'RuntimeDefault'}},
        # libpq hostaddr is unavailable in PgBouncer database config. Keep the
        # certificate hostname while routing only through this verified Service.
        'hostAliases': [{'ip': str(address), 'hostnames': [SOURCE_HOST]}],
        'initContainers': [{
            'name': 'seed-config', 'image': POOLER_IMAGE, 'imagePullPolicy': 'IfNotPresent',
            'command': ['/bin/sh', '-ec', 'umask 077; cp /config-seed/pgbouncer.ini /etc/pgbouncer/pgbouncer.ini; chmod 600 /etc/pgbouncer/pgbouncer.ini'],
            'securityContext': security,
            'resources': {'requests': {'cpu': '10m', 'memory': '16Mi'},
                          'limits': {'cpu': '100m', 'memory': '64Mi'}},
            'volumeMounts': [{'name': 'config-seed', 'mountPath': '/config-seed', 'readOnly': True},
                             {'name': 'config', 'mountPath': '/etc/pgbouncer'}],
        }],
        'containers': [{
            'name': name, 'image': POOLER_IMAGE, 'imagePullPolicy': 'IfNotPresent',
            'securityContext': security, 'ports': [{'containerPort': 5432}],
            'resources': {'requests': {'cpu': '50m', 'memory': '64Mi'},
                          'limits': {'cpu': '250m', 'memory': '256Mi'}},
            'readinessProbe': {'tcpSocket': {'port': 5432}},
            'livenessProbe': {'tcpSocket': {'port': 5432}, 'initialDelaySeconds': 10},
            'volumeMounts': [{'name': 'config', 'mountPath': '/etc/pgbouncer'},
                             {'name': 'tmp', 'mountPath': '/tmp'}] + [
                {'name': n, 'mountPath': p, 'readOnly': True} for n, p in [
                    ('auth', '/etc/pgbouncer-auth'), ('ca', '/etc/cnpg-ca'), ('tls', '/etc/cnpg-tls')]],
        }],
        'volumes': [
            {'name': 'config', 'emptyDir': {}},
            {'name': 'config-seed', 'configMap': {'name': name + '-config'}},
            {'name': 'auth', 'secret': {'secretName': name + '-auth', 'defaultMode': 288}},
            # The cutover executor's bundle of the source and target public CAs (prepare.pooler_trust_patch), so
            # the pooler verifies the backend before and after the host retarget. Naming t8-source-ca here would
            # undo that patch on the next apply and fail TLS once the backend is the target.
            {'name': 'ca', 'secret': {'secretName': 'cutover-backend-ca', 'items': [{'key': 'ca.crt', 'path': 'ca.crt'}]}},
            {'name': 'tls', 'secret': {'secretName': 'target-db-server', 'defaultMode': 288}},
            {'name': 'tmp', 'emptyDir': {}},
        ],
    }
    deploy = resource('apps/v1', 'Deployment', name, spec={
        'replicas': 1, 'strategy': {'type': 'Recreate'}, 'selector': {'matchLabels': selector},
        'template': {'metadata': {'labels': labels,
                                 'annotations': {'bravo.t8/source-egress-service-uid': egress_service_uid}},
                     'spec': pod},
    })
    service = resource('v1', 'Service', name, spec={
        'type': 'ClusterIP', 'selector': selector,
        'ports': [{'name': 'postgres', 'port': 5432, 'targetPort': 5432}],
    })
    return [config, deploy, service]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--egress-address', required=True)
    parser.add_argument('--egress-service-uid', required=True)
    args = parser.parse_args()
    print(json.dumps({'apiVersion': 'v1', 'kind': 'List',
                      'items': pooler(args.egress_address, args.egress_service_uid)}, indent=2))
