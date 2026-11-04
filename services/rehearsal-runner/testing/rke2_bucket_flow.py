"""One-off local-only T11/T15/S3 experiment; NOT a product verifier."""
import argparse
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from string import Template

import boto3
from bravo_agent.contracts import FailureBundle, SnapshotManifest
from bravo_agent.coordinator import Coordinator
from bravo_agent.tools import Snapshot
from bravo_agent.verification import Kube, observe_job_pod

from rehearsal_runner.bucket_probe import observe_bucket_reads
from rehearsal_runner.bucket_proposal_source import BoundBucketProposalSource
from rehearsal_runner.bucket_proposer import T11BucketProposer, bucket_summary
from rehearsal_runner.bucket_s3 import bucket_readers
from rehearsal_runner.bucket_task import prepare_bucket_policy_task
from rehearsal_runner.bucket_verifier import BucketVerifier
from rehearsal_runner.isolation.kube import Kubectl
from rehearsal_runner.isolation.profile import targets
from rehearsal_runner.namespace_lifecycle import NamespaceLifecycle
from rehearsal_runner.runner import T3Gate, run_once

os.umask(0o077)
os.environ['KUBECONFIG'] = '/etc/rancher/rke2/rke2.yaml'
os.environ['PATH'] = '/var/lib/rancher/rke2/bin:/usr/bin:/bin'
parser = argparse.ArgumentParser(description='Run the dedicated local-only T15 bucket fixture')
parser.add_argument('--fixture-id', required=True, help='eight lowercase hexadecimal characters')
parser.add_argument('--report', required=True, help='new private report path')
args = parser.parse_args()
if re.fullmatch(r'[0-9a-f]{8}', args.fixture_id) is None:
    parser.error('invalid fixture id')
report_fd = os.open(args.report, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
TARGET_NS = 'rehearsal-bucket-proof-' + args.fixture_id
BUCKET = 'rehearsal-proof-' + args.fixture_id
POD = 'seaweed'
IMAGE = 'docker.io/amazon/aws-cli@sha256:bc6b7bba44ce38f9604ede49c584824af919047ea03fbcc7c7610671fdef95d8'
CANARY = b't15 local bucket policy test\n'
CANARY_HASH = hashlib.sha256(CANARY).hexdigest()
KUBECTL = '/var/lib/rancher/rke2/bin/kubectl'
SOURCE_PATH, TARGET_PATH = 'src/source-policy.json', 'src/target-policy.json'
public = {'Version': '2012-10-17', 'Statement': [{'Sid': 'PublicRead', 'Effect': 'Allow',
          'Principal': '*', 'Action': 's3:GetObject', 'Resource': Template('arn:aws:s3:::${bucket}/*').substitute(bucket=BUCKET)}]}
restricted = {'Version': '2012-10-17', 'Statement': [{'Sid': 'DenyAnonymousRead', 'Effect': 'Deny',
              'Principal': '*', 'Action': 's3:GetObject', 'Resource': Template('arn:aws:s3:::${bucket}/*').substitute(bucket=BUCKET),
              'Condition': {'StringEquals': {'aws:PrincipalType': 'Anonymous'}}}]}
source, replacement = json.dumps(public, sort_keys=True), json.dumps(restricted, sort_keys=True)
profile = {'SEAWEEDFS_NAMESPACE': TARGET_NS, 'SEAWEEDFS_LABEL_KEY': 'app',
           'SEAWEEDFS_LABEL_VALUE': 't15-bucket-policy-test', 'SEAWEEDFS_PORT': '8333',
           'OPENBAO_NAMESPACE': 'bravo-secrets', 'OPENBAO_LABEL_KEY': 'app.kubernetes.io/instance',
           'OPENBAO_LABEL_VALUE': 'target-openbao', 'OPENBAO_PORT': '8200'}
report = {'environment': 'local-rke2', 'model_provider': 'deterministic_fixture',
          'source_revision': 'synthetic_fixture', 'production_receipt': False}
admin, kubectl = Kube(), Kubectl()
forward = None
signed = None


def require_namespace(namespace):
    meta = admin.get('namespace', namespace.name)['metadata']
    if meta.get('uid') != namespace.uid or meta.get('deletionTimestamp'):
        raise RuntimeError('namespace_identity_changed')


def endpoint_observation(signed_reader, unsigned_reader):
    o = observe_bucket_reads(signed_reader, unsigned_reader, bucket=BUCKET, key='canary',
                             expected_sha256=CANARY_HASH)
    return {'authenticated_before': o.authenticated_before, 'anonymous': o.anonymous,
            'authenticated_after': o.authenticated_after}


class Provider:
    async def propose(self, bundle, tools):
        tools.dispatch('mcp__bravo__repo_read', {'path': SOURCE_PATH})
        target = tools.dispatch('mcp__bravo__repo_read', {'path': TARGET_PATH})
        tools.dispatch('mcp__bravo__plan_propose', {'run_id': bundle.run_id, 'source_sha': bundle.source_sha,
            'rationale': 'Restrict anonymous reads on the dedicated test bucket', 'risk': 'low',
            'edits': [{'path': TARGET_PATH, 'before_sha256': target['sha256'], 'replacement': replacement}]})


class Lifecycle:
    def __init__(self):
        self.inner = NamespaceLifecycle(kubectl, admin, profile)
        self.allow = None

    def create(self, run_id):
        namespace = self.inner.create(run_id)
        try:
            require_namespace(namespace)
            name = 'allow-' + namespace.name
            obj = {'apiVersion': 'networking.k8s.io/v1', 'kind': 'NetworkPolicy',
                   'metadata': {'name': name, 'namespace': TARGET_NS}, 'spec': {
                   'podSelector': {'matchLabels': {'app': 't15-bucket-policy-test'}},
                   'policyTypes': ['Ingress'], 'ingress': [{'from': [{'namespaceSelector': {
                   'matchLabels': {'kubernetes.io/metadata.name': namespace.name}}}],
                   'ports': [{'protocol': 'TCP', 'port': 8333}]}]}}
            created = admin.create(obj)
            self.allow = (name, created['metadata']['uid'])
            report['rehearsal_namespace'] = namespace.name
            report['rehearsal_namespace_uid'] = namespace.uid
            return namespace
        except Exception:
            self.inner.cleanup(namespace)
            raise

    def cleanup(self, namespace):
        allowed_clean = False
        try:
            if self.allow:
                name, uid = self.allow
                admin.call('delete', '--raw', '/apis/networking.k8s.io/v1/namespaces/' + TARGET_NS +
                           '/networkpolicies/' + name, '-f', '-', data={
                               'apiVersion': 'v1', 'kind': 'DeleteOptions', 'preconditions': {'uid': uid}})
                allowed_clean = not admin.call('get', 'networkpolicy', name, '-n', TARGET_NS,
                                               '--ignore-not-found', '-o', 'name').strip()
        finally:
            namespace_clean = self.inner.cleanup(namespace)
        report['namespace_cleanup_confirmed'] = namespace_clean
        report['access_rule_cleanup_confirmed'] = allowed_clean
        return namespace_clean and allowed_clean


APPLY = '''set -eu
aws --endpoint-url "$ENDPOINT" s3api put-bucket-policy --bucket "$BUCKET" --policy file:///candidate/policy.json >/dev/null 2>&1
printf '{"policy_applied":true}\\n'
'''
VERIFY = '''set -u
before=unknown; anon=unknown; after=unknown
if aws --endpoint-url "$ENDPOINT" s3api get-object --bucket "$BUCKET" --key canary /tmp/before >/dev/null 2>&1; then
  if [ "$(sha256sum /tmp/before | cut -d ' ' -f 1)" = "$CANARY_HASH" ]; then before=matches; fi
fi
if aws --endpoint-url "$ENDPOINT" --no-sign-request s3api get-object --bucket "$BUCKET" --key canary /tmp/anonymous >/dev/null 2>/tmp/anonymous.err; then
  anon=allowed
elif grep -Eq '403|AccessDenied' /tmp/anonymous.err; then anon=denied; fi
if aws --endpoint-url "$ENDPOINT" s3api get-object --bucket "$BUCKET" --key canary /tmp/after >/dev/null 2>&1; then
  if [ "$(sha256sum /tmp/after | cut -d ' ' -f 1)" = "$CANARY_HASH" ]; then after=matches; fi
fi
printf '{"authenticated_before":"%s","anonymous":"%s","authenticated_after":"%s"}\\n' "$before" "$anon" "$after"
[ "$before" = matches ] && [ "$anon" = denied ] && [ "$after" = matches ]
'''


def job(namespace, name, script, endpoint):
    env = [{'name': 'ENDPOINT', 'value': endpoint}, {'name': 'BUCKET', 'value': BUCKET},
           {'name': 'CANARY_HASH', 'value': CANARY_HASH}, {'name': 'HOME', 'value': '/tmp'},
           {'name': 'AWS_DEFAULT_REGION', 'value': 'us-east-1'}, {'name': 'AWS_EC2_METADATA_DISABLED', 'value': 'true'},
           {'name': 'AWS_MAX_ATTEMPTS', 'value': '1'}]
    for key, field in [('AWS_ACCESS_KEY_ID', 'access'), ('AWS_SECRET_ACCESS_KEY', 'secret')]:
        env.append({'name': key, 'valueFrom': {'secretKeyRef': {'name': 't15-fixture-auth', 'key': field}}})
    return {'apiVersion': 'batch/v1', 'kind': 'Job', 'metadata': {'name': name, 'namespace': namespace.name},
        'spec': {'backoffLimit': 0, 'activeDeadlineSeconds': 180, 'template': {'spec': {
        'restartPolicy': 'Never', 'automountServiceAccountToken': False,
        'serviceAccountName': 'rehearsal-exec', 'securityContext': {'runAsNonRoot': True,
        'runAsUser': 65532, 'runAsGroup': 65532, 'seccompProfile': {'type': 'RuntimeDefault'}},
        'volumes': [{'name': 'tmp', 'emptyDir': {}}, {'name': 'candidate', 'configMap': {'name': 'candidate-policy'}}],
        'containers': [{'name': 'run', 'image': IMAGE, 'imagePullPolicy': 'IfNotPresent',
        'command': ['/bin/sh', '-c', script], 'env': env,
        'resources': {'requests': {'cpu': '20m', 'memory': '96Mi'}, 'limits': {'cpu': '500m', 'memory': '256Mi'}},
        'securityContext': {'allowPrivilegeEscalation': False, 'readOnlyRootFilesystem': True,
                            'capabilities': {'drop': ['ALL']}},
        'volumeMounts': [{'name': 'tmp', 'mountPath': '/tmp'},
                         {'name': 'candidate', 'mountPath': '/candidate', 'readOnly': True}]}]}}}}


def execute_job(namespace, name, script, endpoint):
    require_namespace(namespace)
    created = admin.create(job(namespace, name, script, endpoint))
    uid = created['metadata']['uid']
    for _ in range(100):
        require_namespace(namespace)
        current = admin.get('job', name, namespace.name)
        if current['metadata']['uid'] != uid:
            raise RuntimeError('job_identity_changed')
        if current.get('status', {}).get('failed', 0):
            raise RuntimeError('job_failed')
        if current.get('status', {}).get('succeeded', 0):
            break
        time.sleep(2)
    else:
        raise RuntimeError('job_timeout')
    execution = observe_job_pod(admin, created, IMAGE, complete=True)
    log = admin.call('logs', 'pod/' + execution['pod_name'], '-n', namespace.name, '-c', 'run')
    after = observe_job_pod(admin, created, IMAGE, complete=True)
    if execution['pod_uid'] != after['pod_uid'] or execution['container_id'] != after['container_id']:
        raise RuntimeError('job_execution_changed')
    require_namespace(namespace)
    report[name + '_execution'] = {key: execution[key] for key in
                                      ('job_uid', 'pod_uid', 'container_id', 'image_id', 'exit_code')}
    return json.loads(log)


class Job:
    def __init__(self, coordinator, credential, target_ip, target_uid):
        self.coordinator, self.credential = coordinator, credential
        self.target_ip, self.target_uid = target_ip, target_uid

    def verify(self, request):
        require_namespace(request.namespace)
        target = admin.get('pod', POD, TARGET_NS)
        if target['metadata']['uid'] != self.target_uid or target['status']['podIP'] != self.target_ip:
            raise RuntimeError('target_identity_changed')
        resolved = BoundBucketProposalSource(self.coordinator, TARGET_PATH).resolve(request)
        if request.target_bucket != BUCKET or json.loads(resolved.replacement) != restricted:
            raise RuntimeError('proposal_outside_fixed_test_policy')
        namespace = request.namespace
        admin.create({'apiVersion': 'v1', 'kind': 'Secret', 'metadata': {'name': 't15-fixture-auth',
                     'namespace': namespace.name}, 'stringData': {'access': self.credential['accessKey'],
                     'secret': self.credential['secretKey']}})
        admin.create({'apiVersion': 'v1', 'kind': 'ConfigMap', 'immutable': True,
                     'metadata': {'name': 'candidate-policy', 'namespace': namespace.name},
                     'data': {'policy.json': resolved.replacement}})
        endpoint = 'http://' + self.target_ip + ':8333'
        applied = execute_job(namespace, 'apply-policy', APPLY, endpoint)
        if applied != {'policy_applied': True}:
            raise RuntimeError('policy_application_unknown')
        observed = execute_job(namespace, 'verify-policy', VERIFY, endpoint)
        require_namespace(namespace)
        if admin.get('pod', POD, TARGET_NS)['metadata']['uid'] != self.target_uid:
            raise RuntimeError('target_identity_changed')
        return observed


try:
    import socket
    if socket.gethostname() != 'bravo-t15-rke2':
        raise RuntimeError('not_local_test_vm')
    meta = admin.get('namespace', TARGET_NS)['metadata']
    target_pod = admin.get('pod', POD, TARGET_NS)
    policies = json.loads(admin.call('get', 'networkpolicy', '-n', TARGET_NS, '-o', 'json'))['items']
    if (meta.get('labels', {}).get('purpose') != 't15-bucket-policy-test'
        or target_pod['metadata'].get('labels', {}).get('app') != 't15-bucket-policy-test'
        or target_pod['spec']['containers'][0]['image'] != 'chrislusf/seaweedfs:4.07'
        or len(policies) != 1 or policies[0]['metadata'].get('name') != 'deny-ingress'
        or policies[0].get('spec', {}).get('podSelector') != {}
        or 'Ingress' not in policies[0].get('spec', {}).get('policyTypes', [])
        or policies[0].get('spec', {}).get('ingress', []) != []):
        raise RuntimeError('test_target_not_isolated')
    data = admin.get('secret', 't15-s3-config', TARGET_NS)['data']['s3.json']
    import base64
    credential = json.loads(base64.b64decode(data))['identities'][0]['credentials'][0]
    forward = subprocess.Popen([KUBECTL, '-n', TARGET_NS, 'port-forward', 'pod/' + POD, ':8333'],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    first = forward.stdout.readline().strip()
    if not first.startswith('Forwarding from 127.0.0.1:'):
        raise RuntimeError('port_forward_unavailable')
    port = int(first.split(':')[1].split()[0])
    session = boto3.Session(aws_access_key_id=credential['accessKey'], aws_secret_access_key=credential['secretKey'])
    signed, unsigned = bucket_readers(session, endpoint_url='http://127.0.0.1:' + str(port), region='us-east-1')
    signed.client.put_bucket_policy(Bucket=BUCKET, Policy=source)
    baseline = endpoint_observation(signed, unsigned)
    report['baseline'] = baseline
    if baseline != {'authenticated_before': 'matches', 'anonymous': 'allowed', 'authenticated_after': 'matches'}:
        raise RuntimeError('public_control_failed')
    with tempfile.TemporaryDirectory(prefix='t15-snapshot-') as directory:
        root = pathlib.Path(directory)
        (root / 'src').mkdir()
        for path in (SOURCE_PATH, TARGET_PATH):
            (root / path).write_text(source)
        files = {path: hashlib.sha256((root / path).read_bytes()).hexdigest() for path in (SOURCE_PATH, TARGET_PATH)}
        sha = '1' * 40
        task = prepare_bucket_policy_task('local-real-flow-1', source, BUCKET, source_sha=sha)
        manifest = SnapshotManifest(source_sha=sha, files=files, editable_paths=[TARGET_PATH])
        bundle = FailureBundle(run_id=task.run_id, source_sha=sha, failure_code='POLICY_TRANSLATION',
                               evidence_digest='c' * 64, summary=bucket_summary(task, SOURCE_PATH, TARGET_PATH))
        coordinator = Coordinator(bundle, Snapshot(root, manifest))
        proposer = T11BucketProposer(coordinator, Provider(), SOURCE_PATH, TARGET_PATH)
        lifecycle = Lifecycle()
        base_gate = T3Gate(kubectl, targets(profile), 'busybox:1.36')

        class RecordedGate:
            def check(self, namespace):
                observed = base_gate.check(namespace)
                report['isolation'] = {'status': 'PASS', 'namespace_uid': observed.namespace_uid,
                                       'probe_run_id': observed.probe_run_id}
                return observed

        gate = RecordedGate()
        verifier = BucketVerifier(task, Job(coordinator, credential, target_pod['status']['podIP'],
                                            target_pod['metadata']['uid']))
        outcome = run_once(task, proposer, lifecycle, gate, verifier)
        report['t15_state'] = outcome.state
        report['t15_reason'] = outcome.reason
        report['t11_proposal_state'] = coordinator.state
        report['proposal_digest'] = coordinator.result().get('proposal_digest')
        if outcome.untrusted_receipt is not None:
            report['observation'] = outcome.untrusted_receipt
        report['success'] = (outcome.state == 'RECEIPT_UNVERIFIED'
                             and outcome.untrusted_receipt == {'authenticated_before': 'matches',
                                  'anonymous': 'denied', 'authenticated_after': 'matches'}
                             and report.get('namespace_cleanup_confirmed') is True
                             and report.get('access_rule_cleanup_confirmed') is True)
except Exception as error:
    report['success'] = False
    report['failure_type'] = type(error).__name__
    if isinstance(error, RuntimeError):
        report['failure_stage'] = str(error)
finally:
    if signed is not None:
        try:
            signed.client.put_bucket_policy(Bucket=BUCKET, Policy=replacement)
            report['fixture_restricted_restored'] = True
        except Exception:
            report['fixture_restricted_restored'] = False
    if forward is not None:
        forward.terminate()
        with suppress(Exception):
            forward.wait(timeout=5)
    report['success'] = report.get('success') is True and report.get('fixture_restricted_restored') is True
    output = args.report
    with os.fdopen(report_fd, 'w', encoding='utf-8') as file:
        json.dump(report, file, indent=2)
    print(json.dumps(report, sort_keys=True))
    print('RESULT_FILE=' + output)

sys.exit(0 if report['success'] else 1)
