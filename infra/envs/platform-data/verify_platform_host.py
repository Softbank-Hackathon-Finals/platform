"""Check platform identities against the real API, plus DB isolation without credentials."""
import copy
import hashlib
import itertools
import json
import subprocess
import time
import uuid

PYTHON = "docker.io/library/python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337"
NETWORK_PROBE = """import json,socket,sys,time
start=time.time_ns()//1000000
try:
 if sys.argv[1]=='dns':
  addresses={row[4][0] for row in socket.getaddrinfo(sys.argv[2],None)}
  outcome='resolved' if sys.argv[3] in addresses else 'unexpected_address'
 else:
  with socket.create_connection((sys.argv[2],int(sys.argv[3])),timeout=3): pass
  outcome='connected'
except TimeoutError: outcome='timeout'
except ConnectionRefusedError: outcome='refused'
except OSError: outcome='unreachable'
end=time.time_ns()//1000000
print(json.dumps({'outcome':outcome,'started_at_ms':start,'finished_at_ms':end,'duration_ms':end-start}))
"""
WINDOW_MS = 60000


def network_verdict(observations, stable):
    """NET-01: a silent drop is evidence only with bounded, successful controls."""
    names = ('target_before', 'subject_dns', 'subject_service', 'attempt', 'target_after')
    if not stable or set(observations) != set(names):
        return 'UNKNOWN'
    ordered = [observations[name] for name in names]
    if any(not isinstance(row.get('started_at_ms'), int) or not isinstance(row.get('finished_at_ms'), int)
           or row['finished_at_ms'] < row['started_at_ms'] for row in ordered):
        return 'UNKNOWN'
    if any(left['finished_at_ms'] > right['started_at_ms'] for left, right in itertools.pairwise(ordered)):
        return 'UNKNOWN'
    if ordered[-1]['finished_at_ms'] - ordered[0]['started_at_ms'] > WINDOW_MS:
        return 'UNKNOWN'
    for name, expected in [('target_before', 'connected'), ('subject_dns', 'resolved'),
                           ('subject_service', 'connected'), ('target_after', 'connected')]:
        if observations[name].get('outcome') != expected:
            return 'UNKNOWN'
    outcome = observations['attempt'].get('outcome')
    if outcome == 'connected':
        return 'FAIL'
    return 'PASS' if outcome in ('timeout', 'refused') else 'UNKNOWN'


def overall_status(checks):
    if any(value is False for value in checks.values()):
        return 'FAIL'
    return 'UNKNOWN' if any(value is None for value in checks.values()) else 'PASS'

K = ["/var/lib/rancher/rke2/bin/kubectl", "--kubeconfig=/etc/rancher/rke2/rke2.yaml"]


def admin(*args):
    return subprocess.check_output(K + list(args), text=True, stderr=subprocess.PIPE, timeout=120)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def network_resources(run_id):
    """Temporary controls grant only DNS and the isolated echo service, never DB access."""
    label = {'bravo.dev/network-probe': run_id}
    allowed = label | {'bravo.dev/db-client': 'runtime'}
    resources = []
    for namespace in ('platform-state', 'platform-collector', 'platform-executor'):
        is_allowed = namespace == 'platform-state'
        account = 'platform-runtime' if is_allowed else namespace
        spec = {
            'serviceAccountName': account, 'automountServiceAccountToken': False, 'restartPolicy': 'Never',
            'securityContext': {'runAsNonRoot': True, 'runAsUser': 10001, 'seccompProfile': {'type': 'RuntimeDefault'}},
            'containers': [{'name': 'probe', 'image': PYTHON,
                'command': ['python', '-c', 'from http.server import HTTPServer,BaseHTTPRequestHandler; HTTPServer(("0.0.0.0",8080),BaseHTTPRequestHandler).serve_forever()'
                            if is_allowed else 'import time; time.sleep(600)'],
                'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                                    'capabilities': {'drop': ['ALL']}},
                'resources': {'requests': {'cpu': '20m', 'memory': '32Mi'}, 'limits': {'cpu': '100m', 'memory': '64Mi'}}}],
        }
        if is_allowed:
            spec['containers'][0]['readinessProbe'] = {'tcpSocket': {'port': 8080}, 'periodSeconds': 1}
        resources.append({'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': run_id, 'namespace': namespace},
            'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 600, 'template': {
                'metadata': {'labels': allowed if is_allowed else label}, 'spec': spec}}})
        if is_allowed:
            rules = {'policyTypes': ['Ingress'], 'ingress': [{'from': [
                {'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': ns}}, 'podSelector': {'matchLabels': label}}
                for ns in ('platform-collector', 'platform-executor')], 'ports': [{'protocol': 'TCP', 'port': 8080}]}]}
        else:
            rules = {'policyTypes': ['Egress'], 'egress': [
                {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'platform-state'}},
                         'podSelector': {'matchLabels': allowed}}], 'ports': [{'protocol': 'TCP', 'port': 8080}]},
                {'to': [{'namespaceSelector': {'matchLabels': {'kubernetes.io/metadata.name': 'kube-system'}},
                         'podSelector': {'matchLabels': {'k8s-app': 'kube-dns'}}}],
                 'ports': [{'protocol': 'UDP', 'port': 53}, {'protocol': 'TCP', 'port': 53}]}]}
        resources.append({'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
            'metadata': {'name': run_id, 'namespace': namespace}, 'spec': {'podSelector': {'matchLabels': label}, **rules}})
    resources.append({'apiVersion': 'v1', 'kind': 'Service', 'metadata': {'name': run_id, 'namespace': 'platform-state'},
        'spec': {'selector': allowed, 'ports': [{'port': 8080, 'targetPort': 8080}]}})
    return resources


def environment_state(pods):
    service = json.loads(admin('get', 'service', 'platform-db-rw', '-n', 'platform-state', '-o', 'json'))
    slices = json.loads(admin('get', 'endpointslices', '-n', 'platform-state', '-l',
                              'kubernetes.io/service-name=platform-db-rw', '-o', 'json'))
    policies, identities = [], {}
    for namespace, pod in pods.items():
        data = json.loads(admin('get', 'networkpolicy', '-n', namespace, '-o', 'json'))
        policies.extend({'namespace': namespace, 'name': item['metadata']['name'], 'spec': item['spec']}
                        for item in data['items'])
        data = json.loads(admin('get', 'pod', pod, '-n', namespace, '-o', 'json'))
        identities[namespace] = data['metadata']['uid']
    return {'service': {'uid': service['metadata']['uid'], 'address': service['spec']['clusterIP']},
            'endpoints': sorted(({'name': item['metadata']['name'], 'endpoints': item['endpoints'], 'ports': item['ports']}
                                 for item in slices['items']), key=lambda item: item['name']),
            'policies': sorted(policies, key=lambda item: (item['namespace'], item['name'])), 'pods': identities}


def observe(namespace, pod, mode, address, port):
    return json.loads(admin('exec', pod, '-n', namespace, '--', 'python', '-c', NETWORK_PROBE, mode, address, str(port)))


def verify_network():
    run_id = 't7-network-' + uuid.uuid4().hex[:12]
    owned, pods, results = [], {}, {}
    try:
        for resource in network_resources(run_id):
            text = subprocess.check_output(K + ['create', '-f', '-', '-o', 'json'], input=json.dumps(resource),
                                           text=True, stderr=subprocess.PIPE, timeout=30)
            created = json.loads(text)
            owned.append((created['kind'], created['metadata']['namespace'], created['metadata']['name'], created['metadata']['uid']))
        for namespace in ('platform-state', 'platform-collector', 'platform-executor'):
            for _ in range(30):
                items = json.loads(admin('get', 'pods', '-l', 'job-name=' + run_id, '-n', namespace, '-o', 'json'))['items']
                if items:
                    break
                time.sleep(1)
            if len(items) != 1:
                raise ValueError('missing_or_ambiguous_probe_pod')
            pods[namespace] = items[0]['metadata']['name']
            admin('wait', '--for=condition=Ready', 'pod/' + pods[namespace], '-n', namespace, '--timeout=90s')
        control_ip = json.loads(admin('get', 'service', run_id, '-n', 'platform-state', '-o', 'json'))['spec']['clusterIP']
        for namespace in ('platform-collector', 'platform-executor'):
            before = environment_state(pods)
            address = before['service']['address']
            observations = {}
            observations['target_before'] = observe('platform-state', pods['platform-state'], 'tcp', address, 5432)
            observations['subject_dns'] = observe(namespace, pods[namespace], 'dns', run_id + '.platform-state.svc.cluster.local', control_ip)
            observations['subject_service'] = observe(namespace, pods[namespace], 'tcp', control_ip, 8080)
            observations['attempt'] = observe(namespace, pods[namespace], 'tcp', address, 5432)
            observations['target_after'] = observe('platform-state', pods['platform-state'], 'tcp', address, 5432)
            stable = before == environment_state(pods)
            results[namespace] = {'verdict': network_verdict(observations, stable), 'observations': observations,
                'environment_unchanged': stable, 'endpoint_sha256': fingerprint(before['service']),
                'policy_sha256': fingerprint(before['policies']), 'subjects_sha256': fingerprint(before['pods']),
                'run_id': run_id, 'window_limit_ms': WINDOW_MS}
        return results
    finally:
        for kind, namespace, name, uid in reversed(owned):
            current = json.loads(admin('get', kind, name, '-n', namespace, '-o', 'json'))
            if current['metadata']['uid'] != uid:
                raise ValueError('probe_ownership_changed')
            admin('delete', kind, name, '-n', namespace, '--wait=true', '--timeout=60s')


def main():
    config = json.loads(admin("config", "view", "--raw", "-o", "json"))
    checks = {}
    for namespace, account in [("platform-state", "platform-runtime"),
                                ("platform-collector", "platform-collector"),
                                ("platform-executor", "platform-executor")]:
        token = admin("create", "token", account, "-n", namespace, "--duration=10m").strip()
        child = copy.deepcopy(config)
        child["users"] = [{"name": "probe", "user": {"token": token}}]
        child["contexts"] = [{"name": "probe", "context": {"cluster": config["contexts"][0]["context"]["cluster"], "user": "probe"}}]
        child["current-context"] = "probe"
        for target, resource in [("platform-state", "secrets"), ("platform-state", "pods"), ("kube-system", "secrets")]:
            p = subprocess.run([K[0], "--kubeconfig=/dev/stdin", "get", resource, "-n", target],
                               input=json.dumps(child), text=True, capture_output=True, check=False, timeout=20)
            checks[f"{account}_denied_{target}_{resource}"] = p.returncode != 0 and "forbidden" in p.stderr.lower()
    network = verify_network()
    for namespace, result in network.items():
        checks[namespace + '_database_network_denied'] = {'PASS': True, 'FAIL': False, 'UNKNOWN': None}[result['verdict']]
    status = overall_status(checks)
    print(json.dumps({'status': status, 'checks': checks, 'network': network,
                      'auth': 'actual_TokenRequest_10m', 'check_count': len(checks)}))
    return 0 if status == 'PASS' else 1



if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"status": "FAILED", "error_type": type(error).__name__}))
        raise SystemExit(1) from None
