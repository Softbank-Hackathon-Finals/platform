"""Bounded receipt upload through an operator-owned SSM loopback tunnel.

No service is installed. The reviewed operator starts one receiver in the pinned
T10 Pod/UID, forwards its loopback port through SSM, uploads one hash-bound body,
and removes only that window's receiver/forward/session. Never expose this port
as a Service, ingress, or public listener: SSM authenticates this carrier.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import signal
import stat
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from .product_execution_contracts import ExecutionIntent, ExecutionUnknown
from .product_execution_mailbox import MAX_INTENT, FileProductExecution, decode, encode
from .subscription_product_seal import MAX_PLAINTEXT


def _port(port):
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('explicit unprivileged loopback port required')


def upload(port, intent, receipt):
    """Call only after verifying the existing SSO session and exact Pod UID.

    The receiver's expected hash/length must have been set through SSM, not from
    an upload header. No credentials or plaintext receipt enter SSM parameters.
    The existing operator dispatcher must verify GitHub before invoking this.
    """
    _port(port)
    intent = ExecutionIntent.model_validate(intent)
    FileProductExecution._receipt(intent, receipt)
    raw = encode(receipt)
    if len(raw) > MAX_PLAINTEXT:
        raise ExecutionUnknown('MAILBOX_FILE_TOO_LARGE')
    hashed = hashlib.sha256(raw).hexdigest()
    expected = {'accepted': True, 'request_id': intent.request_id,
                'sha256': hashed, 'bytes': len(raw)}
    try:
        with httpx.Client(trust_env=False, follow_redirects=False, timeout=30) as client:
            with client.stream('PUT', f'http://127.0.0.1:{port}/receipt/{hashed}',
                               content=raw, headers={'Content-Type': 'application/json'}) as response:
                if response.status_code != 201 or response.headers.get('content-encoding'):
                    raise ValueError()
                result = bytearray()
                for chunk in response.iter_raw(chunk_size=4096):
                    result.extend(chunk)
                    if len(result) > MAX_INTENT:
                        raise ValueError()
                if decode(result) != expected:
                    raise ValueError()
        return expected
    except Exception:
        # An upload acknowledgement loss cannot authorize another model dispatch.
        raise ExecutionUnknown('MAILBOX_DELIVERY_UNCONFIRMED_READ_SAME_RECEIPT') from None


def receive(mailbox, *, port, expected_sha256, expected_bytes, seconds=60, ready=lambda _value: None):
    """Serve at most one request under a hard deadline (main thread, Unix).

    Only the operator chooses the expected digest, size and current intent. An
    untrusted upload cannot select a destination path, request ID, or shell code.
    """
    _port(port)
    if (not isinstance(expected_sha256, str) or re.fullmatch('[a-f0-9]{64}', expected_sha256) is None
            or type(expected_bytes) is not int or not 0 < expected_bytes <= MAX_PLAINTEXT
            or type(seconds) is not int or not 1 <= seconds <= 120):
        raise ValueError('bounded receiver metadata required')
    intent = mailbox.claimed()
    accepted = None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_PUT(self):
            nonlocal accepted
            if (self.path != '/receipt/' + expected_sha256
                    or self.headers.get_all('Content-Length') != [str(expected_bytes)]
                    or self.headers.get_all('Transfer-Encoding')
                    or self.headers.get_all('Content-Encoding')
                    or self.headers.get_all('Content-Type') != ['application/json']):
                self.send_error(400)
                return
            raw = self.rfile.read(expected_bytes)
            if len(raw) != expected_bytes or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256):
                self.send_error(400)
                return
            try:
                mailbox.deliver(intent, decode(raw))
            except Exception:
                self.send_error(409)
                return
            accepted = {'accepted': True, 'request_id': intent.request_id,
                        'sha256': expected_sha256, 'bytes': expected_bytes}
            body = encode(accepted)
            self.send_response(201)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

    def expired(_signum, _frame):
        raise TimeoutError('mailbox receiver deadline')

    class Receiver(HTTPServer):
        def handle_error(self, _request, _client_address):
            # Do not emit tracebacks or request details to SSM/journald.
            pass

    # SIGALRM bounds headers, slow uploads and reads, not just time between reads.
    previous = signal.signal(signal.SIGALRM, expired)
    try:
        signal.alarm(seconds)
        with Receiver(('127.0.0.1', port), Handler) as server:
            server.timeout = seconds
            ready({'ready': True, 'pid': os.getpid(), 'port': port,
                   'request_id': intent.request_id, 'deadline_monotonic': time.monotonic() + seconds})
            server.handle_request()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    if accepted is None:
        raise ExecutionUnknown('MAILBOX_DELIVERY_UNCONFIRMED_READ_SAME_RECEIPT')
    return accepted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'claimed', 'receive', 'receipt-status', 'upload'))
    parser.add_argument('--directory')
    parser.add_argument('--intent')
    parser.add_argument('--receipt')
    parser.add_argument('--port', type=int)
    parser.add_argument('--sha256')
    parser.add_argument('--bytes', type=int)
    parser.add_argument('--seconds', type=int, default=60)
    args = parser.parse_args()
    try:
        if args.action == 'upload':
            def private_input(path, limit):
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                            or info.st_uid != os.geteuid() or info.st_size > limit):
                        raise ValueError()
                    raw = stream.read(limit + 1)
                    if len(raw) > limit:
                        raise ValueError()
                return decode(raw)
            result = upload(args.port, private_input(args.intent, MAX_INTENT),
                            private_input(args.receipt, MAX_PLAINTEXT))
            print(json.dumps(result), flush=True)
            return 0
        mailbox = FileProductExecution(args.directory)
        if args.action == 'prepare':
            raw = sys.stdin.buffer.read(MAX_INTENT + 1)
            if len(raw) > MAX_INTENT:
                raise ValueError()
            mailbox.prepare(decode(raw))
            result = {'prepared': True, 'request_id': mailbox.prepared().request_id}
        elif args.action == 'claimed':
            result = mailbox.claimed().model_dump()
        elif args.action == 'receipt-status':
            value = mailbox.read(mailbox.claimed())
            raw = encode(value)
            result = {'accepted': True, 'request_id': value['request_id'],
                      'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
        else:
            result = receive(mailbox, port=args.port, expected_sha256=args.sha256,
                             expected_bytes=args.bytes, seconds=args.seconds,
                             ready=lambda value: print(json.dumps(value), flush=True))
        print(json.dumps(result), flush=True)
    except Exception:
        print(json.dumps({'accepted': False, 'state': 'UNCONFIRMED', 'redispatch': False}), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
