"""Product caller ports for the existing Actions identity and fixed SSM window.

The trusted T10 host owns admission, registration and its durable dispatch claim.
This module never creates either or substitutes the validation fixture workflow.
"""

import json
import os
import re
import socket
import subprocess
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from threading import Lock
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import boto3
import httpx
from botocore.config import Config

from .subscription_product import stopped
from .subscription_product_host import ProductTunnelCall


@dataclass(frozen=True)
class ActionsProductToken:
    environ: Mapping = field(default_factory=lambda: os.environ, repr=False)
    transport: httpx.BaseTransport | None = field(default=None, repr=False)

    def __call__(self) -> str:
        try:
            if self.environ.get("GITHUB_ACTIONS") != "true":
                raise ValueError("Actions identity required")
            url = urlsplit(self.environ["ACTIONS_ID_TOKEN_REQUEST_URL"])
            credential = self.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]
            if (url.scheme != "https" or not url.hostname
                    or not url.hostname.endswith(".actions.githubusercontent.com")
                    or url.port not in {None, 443} or url.username or url.password or url.fragment
                    or not isinstance(credential, str) or not credential or any(c.isspace() for c in credential)):
                raise ValueError("invalid Actions identity endpoint")
            query = [(k, v) for k, v in parse_qsl(url.query, keep_blank_values=True) if k != "audience"]
            endpoint = urlunsplit(url._replace(query=urlencode(query + [("audience", "sts.amazonaws.com")])))
            with httpx.Client(transport=self.transport, trust_env=False, follow_redirects=False,
                              timeout=httpx.Timeout(20, connect=5)) as client:
                with client.stream("GET", endpoint, headers={"Authorization": "Bearer " + credential}) as response:
                    if response.status_code != 200:
                        raise ValueError("Actions identity unavailable")
                    raw = bytearray()
                    for part in response.iter_bytes(chunk_size=8192):
                        raw.extend(part)
                        if len(raw) > 65536:
                            raise ValueError("oversized identity response")
            token = json.loads(raw)["value"]
            if not isinstance(token, str) or not 1 <= len(token) <= 16384 or any(c.isspace() for c in token):
                raise ValueError("invalid identity token")
            return token
        except Exception:
            raise ValueError("Actions product identity unavailable") from None


@dataclass
class ProductWindowCall:
    call: object = field(repr=False)
    sent: bool = False
    open: bool = True
    _lock: object = field(default_factory=Lock, repr=False)

    def __call__(self, body):
        with self._lock:
            if self.sent or not self.open:
                return 409, stopped("PRODUCT_WINDOW_ALREADY_USED")
            self.sent = True  # Response loss cannot silently admit another request.
        return self.call(body)


def _assert_tunnel_port_free():
    # Do not send a token into an already listening local process. The external
    # execution-owner lock is still required after this preflight reservation.
    try:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 18762))
    except OSError:
        raise RuntimeError("product tunnel port already in use") from None


@contextmanager
def product_window(instance_id, runtime_source_sha, *, ssm=None, transport=None):
    """Use one already-authorized T10 dispatch inside a single fixed SSM window.

This does not obtain controller credentials, register an input, mint a workload
JWT, submit a T15 Job, or retry. T10 supplies request_for with ActionsProductToken
only in a real Actions process. A host outside Actions needs an explicit reviewed
orchestration path; copying its token to a file is not that path.
"""
    if not isinstance(instance_id, str) or re.fullmatch(r"i-[a-f0-9]{17}", instance_id) is None:
        raise ValueError("designated instance required")
    call = ProductWindowCall(ProductTunnelCall(runtime_source_sha, transport=transport))
    region = "ap-northeast-2"
    _assert_tunnel_port_free()
    if ssm is None:
        ssm = boto3.client("ssm", region_name=region, config=Config(
            retries={"total_max_attempts": 1}, connect_timeout=5, read_timeout=20))
    session = None
    process = None
    started = False

    def service(operation):
        command = ssm.send_command(InstanceIds=[instance_id], DocumentName="bravo-subscription-service",
                                   Parameters={"operation": [operation]})["Command"]["CommandId"]
        ssm.get_waiter("command_executed").wait(CommandId=command, InstanceId=instance_id,
                                              WaiterConfig={"Delay": 2, "MaxAttempts": 30})

    try:
        started = True  # A lost start response still requires a stop attempt.
        service("start")
        session = ssm.start_session(Target=instance_id, DocumentName="bravo-subscription-tunnel",
                                    Parameters={"localPortNumber": ["18762"]})
        # The pinned plugin supports the AWS CLI environment handoff and unsets
        # that variable on startup. No session token or Actions bearer in argv.
        plugin_env = {k: v for k, v in os.environ.items()
                      if not k.startswith(("ACTIONS_ID_TOKEN_", "AWS_SSM_START_SESSION_RESPONSE"))
                      and k != "AWS_SSM_PLUGIN_ADDITIONAL_STREAM_HOSTS"}
        plugin_env["AWS_SSM_START_SESSION_RESPONSE_BRAVO"] = json.dumps(session)
        process = subprocess.Popen(["session-manager-plugin", "AWS_SSM_START_SESSION_RESPONSE_BRAVO", region,
                                    "StartSession", "", json.dumps({"Target": instance_id}),
                                    f"https://ssm.{region}.amazonaws.com"], env=plugin_env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        del plugin_env
        for _ in range(30):
            if process.poll() is not None:
                raise RuntimeError("product tunnel unavailable")
            try:
                with socket.create_connection(("127.0.0.1", 18762), timeout=1):
                    break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("product tunnel unavailable")
        yield call
    except Exception:
        raise RuntimeError("product window execution unconfirmed") from None
    finally:
        with call._lock:
            call.open = False
        failures = []
        if process:
            try:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            except Exception:
                failures.append("plugin")
        if session:
            try:
                ssm.terminate_session(SessionId=session["SessionId"])
            except Exception:
                failures.append("session")
        if started:
            try:
                service("stop")
            except Exception:
                failures.append("service")
        if failures:
            raise RuntimeError("product window cleanup unconfirmed") from None
