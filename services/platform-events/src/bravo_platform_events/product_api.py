"""The platform Job caller: a projected token verified with the cluster's pinned keys."""

import re
from dataclasses import dataclass

from bravo_agent.contracts import digest

from .store import EventError
from .workload_identity import WorkloadKeySet, WorkloadSender

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


JOB_PREFIX = "bravo-initial-"


@dataclass(frozen=True)
class KubernetesJobCaller:
    """A platform Job's projected token, verified with the cluster's pinned public keys.

    The principal keeps the broker's STS-shaped digest. Only the initial route accepts it;
    the bound Pod name ties the token to one numeric execution ID chosen by the operator.
    """

    sender: WorkloadSender | WorkloadKeySet

    def __post_init__(self):
        if (type(self.sender) not in (WorkloadSender, WorkloadKeySet)
                or re.fullmatch(r"system:serviceaccount:[a-z0-9-]{1,63}:[a-z0-9-]{1,253}", self.sender.subject) is None):
            raise ValueError("reviewed_job_identity_required")

    @property
    def principal(self):
        return digest(dict(Provider=self.sender.issuer, Audience=self.sender.audience,
                           SubjectFromWebIdentityToken=self.sender.subject))

    def authenticate(self, token, principal):
        claims = self.sender.authenticate(token)
        if principal != self.principal:
            raise EventError("forbidden")
        return claims

    def execution_id(self, claims):
        bound = claims.get("kubernetes.io")
        pod = bound.get("pod") if type(bound) is dict else None
        name = pod.get("name") if type(pod) is dict else None
        _, _, namespace, _ = (self.sender.subject + ":").split(":", 3)
        match = re.fullmatch(re.escape(JOB_PREFIX) + r"([1-9][0-9]{0,19})-[a-z0-9]{5}", name or "")
        if match is None or bound.get("namespace") != namespace:
            raise EventError("forbidden")
        return match[1]
