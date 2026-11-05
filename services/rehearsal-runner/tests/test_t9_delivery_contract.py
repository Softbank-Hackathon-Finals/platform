"""T15's injected OpenBao delivery must match T9's actual renderer output.

Uses the repository renderer, or `T9_RENDER_PY` (for a T9 branch not yet merged). Skips when
the renderer is still the older per-run version.
"""

import json
import os
from pathlib import Path

import pytest

from rehearsal_runner.credential_job import (
    CA_CONFIGMAP,
    CONTROL_NAMESPACE,
    READER_ACCOUNT,
    WRITER_ACCOUNT,
)
from rehearsal_runner.t9_delivery import load_t9_delivery

RENDER = Path(os.environ.get("T9_RENDER_PY", Path(__file__).parents[3] / "infra/envs/target-secrets/render.py"))
NS = "rehearsal-plan1-1"


@pytest.fixture(scope="module")
def delivery():
    try:
        return load_t9_delivery(RENDER)
    except (ValueError, FileNotFoundError):
        pytest.skip("namespace-scoped T9 renderer not available yet")


def test_paths_and_role_follow_the_namespace(delivery):
    assert delivery.remote_path(NS) == f"bravo/rehearsal/{NS}/database"
    assert delivery.role_name(NS) == f"rehearsal-app-reader-{NS}"
    role = delivery.auth_role(NS)
    assert role["bound_service_account_namespaces"] == NS and role["token_policies"] == "rehearsal-app-reader"
    assert role["bound_service_account_names"] == READER_ACCOUNT
    assert all(not isinstance(v, (list, dict)) for v in role.values())  # writer policy accepts scalars only
    json.dumps(role)


def test_init_container_mounts_exactly_the_volumes_t15_defines(delivery):
    init = delivery.init_container(NS)
    mounts = {m["name"]: m["mountPath"] for m in init["volumeMounts"]}
    assert mounts == {"bravo-database": "/run/bravo/database", "openbao-ca": "/openbao/ca",
                      "openbao-token": "/var/run/secrets/openbao"}
    script = " ".join(init["command"])
    assert "ca.crt" in script and "/var/run/secrets/openbao/token" in script
    assert f"role={delivery.role_name(NS)}" in script and delivery.remote_path(NS) in script
    assert init["securityContext"]["runAsNonRoot"] is True


def test_writer_identity_and_ca_names_match(delivery):
    renderer = delivery.renderer
    assert renderer.WRITER_NAMESPACE == CONTROL_NAMESPACE and renderer.WRITER_ACCOUNT == WRITER_ACCOUNT
    assert renderer.WRITER_POD_LABEL == {"app.kubernetes.io/component": "rehearsal-openbao-writer"}
    store = next(i for i in renderer.rehearsal_delivery(NS)["items"] if i["kind"] == "SecretStore")
    assert store["spec"]["provider"]["vault"]["caProvider"]["name"] == CA_CONFIGMAP


def test_control_namespace_is_not_a_rehearsal_namespace(delivery):
    with pytest.raises(ValueError):
        delivery.remote_path(CONTROL_NAMESPACE)
    with pytest.raises(ValueError):
        delivery.remote_path("rehearsal-control")
