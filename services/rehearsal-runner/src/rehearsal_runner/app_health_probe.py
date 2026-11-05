"""Observe the demo-chat DB health endpoint without reading DB credentials."""

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .credential_probe import Observation

_DNS_LABEL = re.compile(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def observe_app_database(namespace: str, service: str, port: int, *, timeout: float = 5) -> Observation:
    """Inspect only the known health response on the scoped in-cluster service.

    A missing app, unknown response, proxy or transport failure cannot establish
    DB success or failure. This observation is not a verification receipt.
    """
    if (not isinstance(namespace, str) or _DNS_LABEL.fullmatch(namespace) is None
            or not isinstance(service, str) or _DNS_LABEL.fullmatch(service) is None
            or type(port) is not int or not 1 <= port <= 65535
            or not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 10):
        raise ValueError("invalid app probe configuration")
    url = f"http://{service}.{namespace}.svc.cluster.local:{port}/api/health/db"
    request = Request(url, method="GET")
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(1025)
            if response.status != 200 or len(body) > 1024:
                return "unknown"
            parsed = json.loads(body)
            return "yes" if isinstance(parsed, dict) and list(parsed) == ["ok"] and parsed["ok"] is True else "unknown"
    except HTTPError as error:
        try:
            if error.code != 503:
                return "unknown"
            body = error.read(1025)
            if len(body) > 1024:
                return "unknown"
            return "no" if json.loads(body) == {"detail": "database unavailable"} else "unknown"
        except (OSError, ValueError, UnicodeError):
            return "unknown"
        finally:
            error.close()
    except (OSError, URLError, ValueError, UnicodeError):
        return "unknown"
