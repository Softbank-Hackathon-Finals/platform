"""Update only a registered LB's pool weights. Never create or enable resources."""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass

import httpx


class EdgeError(RuntimeError):
    """Stable error codes only: no provider bodies, credentials or resource IDs."""


@dataclass(frozen=True)
class Binding:
    zone_id: str
    lb_id: str
    hostname: str
    source_pool: str
    target_pool: str

    def __post_init__(self):
        for value in (self.zone_id, self.lb_id, self.source_pool, self.target_pool):
            if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
                raise ValueError("invalid_binding_id")
        if self.source_pool == self.target_pool:
            raise ValueError("distinct_pools_required")
        if (not isinstance(self.hostname, str) or len(self.hostname) > 253 or
                any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in self.hostname.split("."))):
            raise ValueError("invalid_hostname")

    def weight(self, result: object) -> int:
        """Fail closed on steering overrides, disabled resources or identity drift."""
        if not isinstance(result, dict):
            raise EdgeError("invalid_provider_result")
        required = {
            "id": self.lb_id, "name": self.hostname, "enabled": True, "proxied": True,
            "steering_policy": "random", "session_affinity": "none",
            "default_pools": [self.source_pool, self.target_pool],
            "fallback_pool": self.source_pool,
        }
        if any(result.get(key) != value for key, value in required.items()):
            raise EdgeError("binding_or_steering_drift")
        if result.get("enabled") is not True or result.get("proxied") is not True:
            raise EdgeError("binding_or_steering_drift")
        if any(result.get(key) for key in (
            "rules", "country_pools", "region_pools", "pop_pools", "networks", "location_pools"
        )):
            raise EdgeError("steering_override_present")
        random = result.get("random_steering")
        if (not isinstance(random, dict) or type(random.get("default_weight")) not in (int, float)
                or random["default_weight"] != 0):
            raise EdgeError("invalid_random_steering")
        weights = random.get("pool_weights")
        if not isinstance(weights, dict) or set(weights) != {self.source_pool, self.target_pool}:
            raise EdgeError("pool_set_mismatch")
        source, target = weights[self.source_pool], weights[self.target_pool]
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
               for value in (source, target)) or source + target <= 0:
            raise EdgeError("invalid_pool_weights")
        share = 100 * target / (source + target)
        for allowed in (0, 10, 50, 100):
            if math.isclose(share, allowed, abs_tol=1e-8):
                return allowed
        raise EdgeError("unsupported_pool_weights")


class Cloudflare:
    def __init__(self, binding: Binding, token: Callable[[], str], *,
                 transport: httpx.BaseTransport | None = None):
        self.binding = binding
        self._token = token
        # No redirects, ambient proxies, retries, custom origins or TLS-disable switch.
        self._client = httpx.Client(timeout=10, follow_redirects=False, trust_env=False,
                                    transport=transport)
        self._url = ("https://api.cloudflare.com/client/v4/zones/" + binding.zone_id
                     + "/load_balancers/" + binding.lb_id)

    def close(self):
        self._client.close()

    def _request(self, method: str, payload: dict | None = None) -> int:
        try:
            token = self._token().strip()
            if not token or any(ch.isspace() for ch in token):
                raise ValueError
            response = self._client.request(method, self._url, json=payload,
                                            headers={"Authorization": "Bearer " + token})
            if response.status_code != 200:
                raise EdgeError("provider_http_error")
            document = response.json()
            if not isinstance(document, dict) or document.get("success") is not True:
                raise EdgeError("provider_unsuccessful")
            return self.binding.weight(document.get("result"))
        except EdgeError:
            raise
        except (httpx.HTTPError, ValueError, TypeError, OSError):
            raise EdgeError("provider_response_unknown") from None

    def read(self) -> int:
        return self._request("GET")

    def set_weight(self, weight: int) -> None:
        if type(weight) is not int or weight not in (0, 10, 50, 100):
            raise ValueError("unsupported_weight")
        acknowledged = self._request("PATCH", {"random_steering": {
            "default_weight": 0,
            "pool_weights": {self.binding.source_pool: (100 - weight) / 100,
                             self.binding.target_pool: weight / 100},
        }})
        if acknowledged != weight:
            raise EdgeError("provider_ack_mismatch")
