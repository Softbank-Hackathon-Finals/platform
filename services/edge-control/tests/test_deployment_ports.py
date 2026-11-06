"""SHIFT host ports: operator SSO principal and the plan's current GATE session, from one protected JSON."""

import json
import sys
import types

import pytest

from bravo_edge_control.deployment_ports import create_ports
from bravo_edge_control.runtime import RuntimePorts

ROLE = "AWSReservedSSO_BravoOperator_0123456789abcdef"
SSO = dict(region="ap-northeast-2", expected_account="123456789012",
           expected_caller_arn="arn:aws:sts::123456789012:assumed-role/" + ROLE + "/example-human",
           expected_sso_role_arn="arn:aws:iam::123456789012:role/aws-reserved/sso.amazonaws.com/ap-northeast-2/" + ROLE,
           expected_sso_role_id="AROA" + "X" * 17, tenant_id="team")


def test_ports_come_from_the_gate_factory_and_operator_sso(tmp_path, monkeypatch):
    calls = []

    class View:
        def session(self, service, readiness):
            return ("session", service, readiness)

    class Records:
        def view(self, c, actor, plan):
            calls.append((c, actor, plan))
            return View()

    module = types.ModuleType("fake_gate")
    module.factory = lambda store: (Records(), lambda view: ("service", "readiness"))
    monkeypatch.setitem(sys.modules, "fake_gate", module)
    config = tmp_path / "edge-ports.json"
    config.write_text(json.dumps({"gate": "fake_gate:factory", "operator_sso": SSO, "sso_profile": "bravo"}))
    ports = create_ports(object(), str(config))
    assert isinstance(ports, RuntimePorts)
    assert ports.load_session("c", "actor", {"plan_id": "p"}) == ("session", "service", "readiness")
    assert calls == [("c", "actor", {"plan_id": "p"})]


def test_unknown_fields_are_refused(tmp_path):
    config = tmp_path / "edge-ports.json"
    config.write_text(json.dumps({"gate": "x:y", "operator_sso": SSO, "sso_profile": "bravo", "token": "no"}))
    with pytest.raises(ValueError, match="edge_deployment_ports_configuration_invalid"):
        create_ports(object(), str(config))
