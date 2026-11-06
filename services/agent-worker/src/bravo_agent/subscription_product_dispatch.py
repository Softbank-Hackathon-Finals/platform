"""Operator-only gh transport: persist before one dispatch; thereafter read that run."""

import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .contracts import digest
from .product_execution_contracts import (
    ARTIFACT,
    REPOSITORY,
    WORKFLOW,
    ExecutionIntent,
    validate_execution_receipt,
)
from .product_execution_contracts import ExecutionPending as ExecutionPending
from .product_execution_contracts import ExecutionUnknown as ExecutionUnknown
from .subscription_product_seal import open_receipt

MAX_ARTIFACT = 2 * 1024 * 1024


class DispatchUnconfirmed(ExecutionUnknown):
    """A bounded provider response for the private ledger, never console output."""

    def __init__(self, response):
        super().__init__('GITHUB_DISPATCH_UNCONFIRMED')
        self.response = response


class OperatorGitHub:
    """Uses the operator's existing gh login; never installs credentials in a Pod."""

    def _call(self, args, payload=None):
        try:
            result = subprocess.run(['gh', 'api', *args], input=payload, capture_output=True, timeout=60, check=False)
            if result.returncode or len(result.stdout) > 8 * 1024 * 1024:
                raise ExecutionUnknown('GITHUB_RESPONSE_UNCONFIRMED')
            return result.stdout
        except Exception:
            raise ExecutionUnknown('GITHUB_RESPONSE_UNCONFIRMED') from None

    def get(self, path):
        try:
            return json.loads(self._call(['--method', 'GET', path]))
        except Exception:
            raise ExecutionUnknown('GITHUB_RESPONSE_UNCONFIRMED') from None

    def dispatch(self, workflow_id, inputs):
        # Retain HTTP rejection evidence before the generic at-most-once boundary.
        # stderr can contain authentication diagnostics; it never enters the ledger.
        payload = json.dumps({'ref': 'develop', 'inputs': inputs, 'return_run_details': True}).encode()
        response = {'observed_at': datetime.now(UTC).isoformat(), 'http_status': None,
                    'github_request_id': None, 'body': None, 'transport': 'unconfirmed'}
        try:
            result = subprocess.run(['gh', 'api', '--include', '--method', 'POST',
                '-H', 'X-GitHub-Api-Version: 2022-11-28',
                f'repos/{REPOSITORY}/actions/workflows/{workflow_id}/dispatches', '--input', '-'],
                input=payload, capture_output=True, timeout=60, check=False)
        except Exception:
            raise DispatchUnconfirmed(response) from None
        if len(result.stdout) <= 65536:
            raw = result.stdout.decode('utf-8', errors='replace').replace('\r\n', '\n')
            headers, separator, body = raw.partition('\n\n')
            lines = headers.splitlines()
            match = re.fullmatch(r'HTTP/[0-9.]+ ([0-9]{3})(?: .*)?', lines[0]) if lines else None
            if match and separator:
                response['http_status'] = int(match[1])
                for header in lines[1:]:
                    key, _, value = header.partition(':')
                    if key.lower() == 'x-github-request-id' and re.fullmatch(r'[A-Za-z0-9:-]{1,128}', value.strip()):
                        response['github_request_id'] = value.strip()
                # POST has only opaque execution metadata, never JWTs or source.
                # Preserve its error body privately; do not serialize other headers.
                response['body'] = body
                response['transport'] = 'http_response'
        if result.returncode or response['http_status'] not in (200, 204):
            raise DispatchUnconfirmed(response)
        return response

    def download(self, artifact_id):
        data = self._call(['--method', 'GET', f'repos/{REPOSITORY}/actions/artifacts/{artifact_id}/zip'])
        if len(data) > MAX_ARTIFACT:
            raise ExecutionUnknown('ARTIFACT_TOO_LARGE')
        return data


def _private_file(path):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    os.close(fd)


def _dispatch_run_id(response):
    if not isinstance(response, dict) or response.get('http_status') != 200:
        return None
    try:
        body = json.loads(response['body'])
    except (KeyError, TypeError, ValueError):
        return None
    run_id = body.get('workflow_run_id') if isinstance(body, dict) else None
    return str(run_id) if type(run_id) is int and run_id > 0 else None


class OperatorProductExecution:
    """T10 owns the admission claim. This ledger owns only its external send receipt.

    Repeating submit, losing its HTTP response, or restarting never sends again.
    No scheduler, background dispatcher, retry, credentials, or model runs here.
    """

    _parse_intent = staticmethod(ExecutionIntent.model_validate)
    _open_receipt = staticmethod(open_receipt)
    _validate_receipt = staticmethod(validate_execution_receipt)
    workflow = WORKFLOW

    def __init__(self, state_dir, github=None):
        self.root = Path(state_dir)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_mode & 0o077:
            raise ValueError('private execution directory required')
        self.path = self.root / 'executions.sqlite3'
        try:
            _private_file(self.path)
        except FileExistsError:
            pass
        mode = self.path.lstat().st_mode
        if not stat.S_ISREG(mode) or mode & 0o077:
            raise ValueError('private execution ledger required')
        self.github = github or OperatorGitHub()
        with self._db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS executions (request_id TEXT PRIMARY KEY, intent TEXT NOT NULL, metadata TEXT NOT NULL, run_id TEXT)')

    def _db(self):
        return sqlite3.connect(self.path, timeout=15)

    def _private_key(self, request_id, *, create=False):
        if not isinstance(request_id, str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}', request_id) is None:
            raise ValueError('invalid execution request ID')
        path = self.root / ('recipient-' + request_id + '.key')
        if create and not path.exists():
            with self._db() as db:
                sent = db.execute('SELECT 1 FROM executions WHERE request_id=?', (request_id,)).fetchone()
            if sent:
                raise ExecutionUnknown('EXECUTION_KEY_UNAVAILABLE')
            # Publish a fully fsynced private key atomically; concurrent prepares use the winner.
            fd, temporary = tempfile.mkstemp(prefix='.recipient-', dir=self.root)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(X25519PrivateKey.generate().private_bytes_raw())
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                os.unlink(temporary)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                mode = os.fstat(stream.fileno()).st_mode
                if not stat.S_ISREG(mode) or mode & 0o077:
                    raise ValueError('private key file required')
                raw = stream.read(33)
            return X25519PrivateKey.from_private_bytes(raw)
        except Exception:
            raise ExecutionUnknown('EXECUTION_KEY_UNAVAILABLE') from None

    def recipient(self, request_id):
        """Prepare a local decryption key once, returning only its public key to T10."""
        return self._private_key(request_id, create=True).public_key().public_bytes_raw().hex()

    def _record(self, intent):
        with self._db() as db:
            row = db.execute('SELECT intent, metadata, run_id FROM executions WHERE request_id=?', (intent.request_id,)).fetchone()
        if row is None:
            raise ExecutionUnknown('EXECUTION_NOT_SUBMITTED')
        if json.loads(row[0]) != intent.model_dump():
            raise ExecutionUnknown('EXECUTION_INTENT_CONFLICT')
        return json.loads(row[1]), row[2]

    def submit(self, intent):
        intent = self._parse_intent(intent)
        with self._db() as db:
            exists = db.execute('SELECT 1 FROM executions WHERE request_id=?', (intent.request_id,)).fetchone()
        if exists:
            return self.read(intent)
        if self._private_key(intent.request_id).public_key().public_bytes_raw().hex() != intent.recipient_public_key:
            raise ExecutionUnknown('EXECUTION_RECIPIENT_CONFLICT')
        actor = self.github.get('user')
        head = self.github.get(f'repos/{REPOSITORY}/git/ref/heads/develop')
        workflow = self.github.get(f'repos/{REPOSITORY}/actions/workflows/{self.workflow}')
        if (head.get('object', {}).get('sha') != intent.runtime_source_sha
                or workflow.get('path') != f'.github/workflows/{self.workflow}' or workflow.get('state') != 'active'
                or type(workflow.get('id')) is not int or type(actor.get('id')) is not int):
            raise ExecutionUnknown('WORKFLOW_OR_SOURCE_NOT_READY')
        metadata = {'actor_id': actor['id'], 'workflow_id': workflow['id'],
                    'dispatch_sent_at': datetime.now(UTC).isoformat()}
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            fresh = db.execute('INSERT OR IGNORE INTO executions VALUES (?,?,?,NULL)',
                               (intent.request_id, json.dumps(intent.model_dump()), json.dumps(metadata))).rowcount == 1
        if fresh:
            # This committed marker deliberately survives a lost response or process crash.
            try:
                response = self.github.dispatch(workflow['id'], intent.model_dump())
            except DispatchUnconfirmed as error:
                self._save_dispatch_response(intent.request_id, metadata, error.response)
                raise ExecutionUnknown('DISPATCH_RESPONSE_UNCONFIRMED_READ_SAME_EXECUTION') from None
            except Exception:
                raise ExecutionUnknown('DISPATCH_RESPONSE_UNCONFIRMED_READ_SAME_EXECUTION') from None
            else:
                if response is not None:
                    self._save_dispatch_response(intent.request_id, metadata, response)
        return self.read(intent)

    def _save_dispatch_response(self, request_id, metadata, response):
        # This is evidence, not permission to dispatch again even on a known 4xx.
        with self._db() as db:
            db.execute('UPDATE executions SET metadata=?, run_id=COALESCE(run_id,?) WHERE request_id=?',
                       (json.dumps(metadata | {'dispatch_response': response}), _dispatch_run_id(response), request_id))

    def _check_run(self, intent, metadata, run):
        if (run.get('event') != 'workflow_dispatch' or run.get('head_branch') != 'develop'
                or run.get('head_sha') != intent.runtime_source_sha or run.get('run_attempt') != 1
                or run.get('workflow_id') != metadata['workflow_id']
                or run.get('actor', {}).get('id') != metadata['actor_id']
                or run.get('head_repository', {}).get('full_name') != REPOSITORY
                or run.get('display_title') != 'product-' + intent.request_id
                or type(run.get('id')) is not int):
            raise ExecutionUnknown('RUN_BINDING_CONFLICT')

    def _find_run(self, intent, metadata, allow_missing=False):
        created = (datetime.fromisoformat(metadata['dispatch_sent_at']) - timedelta(seconds=5)).isoformat()
        matches = []
        for page in range(1, 11):
            query = urlencode({'event': 'workflow_dispatch', 'branch': 'develop', 'created': '>=' + created,
                               'per_page': 100, 'page': page})
            data = self.github.get(f'repos/{REPOSITORY}/actions/workflows/{metadata["workflow_id"]}/runs?{query}')
            runs = data['workflow_runs']
            matches.extend(run for run in runs if run.get('display_title') == 'product-' + intent.request_id)
            if len(runs) < 100:
                break
        else:
            raise ExecutionUnknown('RUN_SEARCH_INCOMPLETE')
        if not matches:
            if allow_missing:
                return None
            raise ExecutionPending('RUN_NOT_YET_OBSERVED')
        if len(matches) != 1:
            raise ExecutionUnknown('MULTIPLE_RUNS_FOR_INTENT')
        self._check_run(intent, metadata, matches[0])
        return matches[0]

    def read(self, intent):
        intent = self._parse_intent(intent)
        metadata, saved_id = self._record(intent)
        acknowledged_id = _dispatch_run_id(metadata.get('dispatch_response'))
        if acknowledged_id is not None and saved_id is not None and acknowledged_id != saved_id:
            raise ExecutionUnknown('RUN_BINDING_CONFLICT')
        private = self._private_key(intent.request_id)
        if private.public_key().public_bytes_raw().hex() != intent.recipient_public_key:
            raise ExecutionUnknown('EXECUTION_RECIPIENT_CONFLICT')
        # Keep duplicate/incomplete-search rejection. A durable ID also permits
        # exact lookup while the provider's list has not indexed that run yet.
        run = self._find_run(intent, metadata, saved_id is not None)
        if run is not None and saved_id is not None and str(run['id']) != saved_id:
            raise ExecutionUnknown('RUN_BINDING_CONFLICT')
        run_id = run['id'] if run is not None else int(saved_id)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE executions SET run_id=? WHERE request_id=? AND run_id IS NULL', (str(run_id), intent.request_id))
            bound_id = db.execute('SELECT run_id FROM executions WHERE request_id=?', (intent.request_id,)).fetchone()[0]
            if bound_id != str(run_id):
                raise ExecutionUnknown('RUN_BINDING_CONFLICT')
        run = self.github.get(f'repos/{REPOSITORY}/actions/runs/{run_id}')
        self._check_run(intent, metadata, run)
        if run['id'] != run_id:
            raise ExecutionUnknown('RUN_BINDING_CONFLICT')
        if run.get('status') != 'completed':
            raise ExecutionPending('RUN_IN_PROGRESS')
        if run.get('conclusion') != 'success':
            raise ExecutionUnknown('RUN_DID_NOT_COMPLETE_SUCCESSFULLY')
        data = self.github.get(f'repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100')
        artifacts = data['artifacts']
        if data.get('total_count') != len(artifacts):
            raise ExecutionUnknown('ARTIFACT_LIST_INCOMPLETE')
        matching = [a for a in artifacts if a.get('name') == ARTIFACT]
        if len(matching) != 1:
            raise ExecutionUnknown('ARTIFACT_UNCONFIRMED')
        artifact = matching[0]
        if (artifact.get('expired') is not False or type(artifact.get('id')) is not int
                or type(artifact.get('size_in_bytes')) is not int or not 0 < artifact['size_in_bytes'] <= MAX_ARTIFACT
                or artifact.get('workflow_run', {}).get('id') != run_id
                or artifact.get('workflow_run', {}).get('head_sha') != intent.runtime_source_sha
                or not isinstance(artifact.get('digest'), str)
                or re.fullmatch(r'sha256:[a-f0-9]{64}', artifact['digest']) is None):
            raise ExecutionUnknown('ARTIFACT_BINDING_CONFLICT')
        raw = self.github.download(artifact['id'])
        if len(raw) > MAX_ARTIFACT or 'sha256:' + hashlib.sha256(raw).hexdigest() != artifact['digest']:
            raise ExecutionUnknown('ARTIFACT_DIGEST_MISMATCH')
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = archive.infolist()
                if (len(entries) != 1 or entries[0].filename != 'product-result.json'
                        or entries[0].file_size > MAX_ARTIFACT or entries[0].flag_bits & 1):
                    raise ValueError('unexpected artifact member')
                with archive.open(entries[0]) as stream:
                    payload = stream.read(MAX_ARTIFACT + 1)
                if len(payload) > MAX_ARTIFACT:
                    raise ValueError('oversized receipt')
            receipt = self._validate_receipt(intent, run_id,
                self._open_receipt(intent, run_id, json.loads(payload), private))
        except Exception:
            raise ExecutionUnknown('ARTIFACT_RECEIPT_INVALID') from None
        return receipt


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['recipient', 'submit', 'read'])
    parser.add_argument('--intent', type=Path)
    parser.add_argument('--request-id')
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        client = OperatorProductExecution(args.state_dir)
        if args.operation == 'recipient':
            print(json.dumps({'recipient_public_key': client.recipient(args.request_id)}))
            return 0
        if args.intent is None or args.output is None:
            raise ValueError('intent and output required')
        intent = ExecutionIntent.model_validate_json(args.intent.read_bytes())
        receipt = getattr(client, args.operation)(intent)
        # Existing output must be explicitly read/compared by the caller, never overwritten.
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(receipt, stream, allow_nan=False)
        print(json.dumps({'state': 'COMPLETE', 'github_run_id': receipt['github_run_id'], 'receipt_digest': digest(receipt)}))
        return 0
    except ExecutionPending as error:
        print(json.dumps({'state': 'PENDING', 'reason': str(error)}))
        return 4
    except Exception:
        print(json.dumps({'state': 'UNKNOWN', 'reason': 'READ_SAME_EXECUTION_NO_REDISPATCH'}))
        return 5


if __name__ == '__main__':
    raise SystemExit(main())
