"""Deployment ports for the SHIFT host (`bravo_edge_control.run --ports bravo_edge_control.deployment_ports:create_ports`).

One protected JSON (BRAVO_EDGE_PORTS, default /runtime/edge-ports.json) names only:

    {"gate": "module:factory",            # T19: factory(store) -> (GateRecords, load_gate), as the approval host
     "operator_sso": {region, expected_account, expected_caller_arn, expected_sso_role_arn,
                      expected_sso_role_id, tenant_id},
     "sso_profile": "<existing AWS SSO profile of the operator running SHIFT>"}

- principal: the operator who runs SHIFT proves the team's AWS SSO identity for the operator audience
  each time (short-lived, in memory, never printed); OperatorSsoVerifier maps it to the subject that
  OperatorAccess grants approve_shift/execute_shift and gate:evaluate/gate:consume to.
- load_session: the plan's current GATE session from GateRecords and T19's load_gate, in the caller's
  plan transaction (ShiftAuthority re-evaluates inside served_reevaluation).
"""

import importlib
import json
import os
from pathlib import Path

from .runtime import RuntimePorts

CONFIG = "/runtime/edge-ports.json"


def create_ports(store, config_file=None):
    from bravo_platform_events.operator_identity import OPERATOR_SSO_AUDIENCE, OperatorSsoVerifier
    from bravo_platform_events.registration_sso import (
        SsoRegistrationVerifier,
        make_registration_token,
    )

    config = json.loads(Path(config_file or os.environ.get("BRAVO_EDGE_PORTS", CONFIG)).read_text())
    if type(config) is not dict or set(config) != {"gate", "operator_sso", "sso_profile"}:
        raise ValueError("edge_deployment_ports_configuration_invalid")
    verifier = OperatorSsoVerifier(SsoRegistrationVerifier(**(config["operator_sso"] | {"audience": OPERATOR_SSO_AUDIENCE})))
    module, name = config["gate"].split(":")
    records, load_gate = getattr(importlib.import_module(module), name)(store)

    def principal():
        token = make_registration_token(profile_name=config["sso_profile"], region=config["operator_sso"]["region"],
                                        audience=OPERATOR_SSO_AUDIENCE)
        actor = verifier.verify(token)
        if actor is None:
            raise ValueError("operator_identity_unverified")
        return actor

    def load_session(c, actor, plan):
        view = records.view(c, actor, plan)
        service, readiness = load_gate(view)
        return view.session(service, readiness)

    return RuntimePorts(principal=principal, load_session=load_session)
