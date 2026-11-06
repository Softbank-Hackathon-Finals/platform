"""HTTP client using the existing personal credential wrapper; no kubeconfig/Secret export."""
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from uuid import uuid4

PROXY = "/api/v1/namespaces/target-dev/services/http:bravo-object-operations:http/proxy"
BASE = "/v1/object-exposure/runs"
AUDIENCE = "bravo-object-operations"


def service_token():
    """Mint a separate audience token with the caller's existing personal context."""
    try:
        identity = subprocess.run(["kubectl", "auth", "whoami", "-o", "json"],
                                  capture_output=True, text=True, timeout=20, check=True)
        subject = json.loads(identity.stdout)["status"]["userInfo"]["username"]
        if not re.fullmatch(r"system:serviceaccount:bravo-team-access:member-0[1-5]", subject):
            raise APIError(403, {"error": "personal_member_required"})
        token = subprocess.run(["kubectl", "create", "token", subject.rsplit(":", 1)[1],
                                "--namespace=bravo-team-access", "--audience=" + AUDIENCE, "--duration=10m"],
                               capture_output=True, text=True, timeout=20, check=True).stdout.strip()
        if not token:
            raise ValueError("empty token")
        return token
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        raise APIError(401, {"error": "service_token_unavailable"}) from None


class APIError(Exception):
    def __init__(self, status, value):
        self.status, self.value = status, value
        super().__init__(value.get("error", "api_unavailable"))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect_refused", headers, fp)


class Client:
    def __init__(self, profile):
        self.origin = profile["direct_server"].rstrip("/") + PROXY
        self.token = os.environ.get("BRAVO_TEAM_DIRECT_TOKEN")
        if not self.token:
            raise APIError(401, {"error": "personal_wrapper_required"})
        self.service_token = service_token()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    @staticmethod
    def new_key():
        return str(int(time.time())) + "-" + uuid4().hex

    @staticmethod
    def run_id(owner, key):
        return "objects-" + hashlib.sha256((owner + "/" + key).encode()).hexdigest()[:32]

    def call(self, method, suffix="", body=None, key=None, route=None):
        headers = {"Authorization": "Bearer " + self.token,
                   "X-Bravo-Authorization": "Bearer " + self.service_token,
                   "Content-Type": "application/json"}
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(self.origin + (route or BASE + suffix), method=method,
                                         data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with self.opener.open(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            try:
                value = json.loads(error.read(8192))
                if "error" not in value:
                    value = {"error": "api_access_denied" if error.code in (401, 403) else "api_unavailable"}
            except ValueError:
                value = {"error": "api_unavailable"}
            raise APIError(error.code, value) from None
        except OSError:
            raise APIError(503, {"error": "response_unknown_query_same_run_id"}) from None
