"""Read only the HTTP status of a scoped Kubernetes Secret GET.

Run each probe under the service account being tested. The response body and
bearer token are never returned or logged. This is an observation, not a T38
verification receipt or proof of a namespace UID.
"""

import re
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request

from .credential_probe import Observation

_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")
_DNS_SUBDOMAIN = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?")


def observe_secret_get(
    namespace: str,
    secret_name: str,
    *,
    token_path: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token"),
    ca_path: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"),
    timeout: float = 5,
) -> Observation:
    """Return yes on 200, no on 403, and unknown on all ambiguous outcomes.

    The fixed in-cluster service DNS name, verified CA, projected SA token and
    redirect rejection prevent model-controlled endpoints receiving the token.
    A positive control must independently prove the fixture exists; this function
    alone must not interpret a 403 as an end-to-end verification success.
    """
    if (not isinstance(namespace, str) or _DNS_LABEL.fullmatch(namespace) is None
            or not isinstance(secret_name, str) or len(secret_name) > 253
            or _DNS_SUBDOMAIN.fullmatch(secret_name) is None
            or any(_DNS_LABEL.fullmatch(part) is None for part in secret_name.split("."))
            or not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 10):
        raise ValueError("invalid secret probe configuration")
    try:
        token = token_path.read_text(encoding="utf-8").strip()
        if not token or len(token) > 16384 or any(c.isspace() for c in token):
            return "unknown"
        context = ssl.create_default_context(cafile=str(ca_path))
        url = ("https://kubernetes.default.svc/api/v1/namespaces/" + quote(namespace, safe="")
               + "/secrets/" + quote(secret_name, safe=""))
        request = Request(url, headers={"Authorization": "Bearer " + token}, method="GET")
        # A redirected authenticated request must never reach a different host.
        # urllib follows redirects by default, so use a custom opener below.
        return _request_status(request, context, timeout)
    except (OSError, UnicodeError, ValueError, ssl.SSLError):
        return "unknown"


def _request_status(request: Request, context: ssl.SSLContext, timeout: float) -> Observation:
    from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, build_opener

    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            return "yes" if response.status == 200 else "unknown"
    except HTTPError as error:
        try:
            return "no" if error.code == 403 else "unknown"
        finally:
            error.close()
    except (OSError, URLError, ValueError):
        return "unknown"
