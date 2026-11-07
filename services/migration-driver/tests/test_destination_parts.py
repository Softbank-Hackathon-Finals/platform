"""The parts that run on a host or hold recovery material, without a cluster."""

import ipaddress
import json
import platform
import stat
import subprocess

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from bravo_migration.destination import _ca, _leaf, _openbao_tls
from bravo_migration.host import host_paths_script
from bravo_migration.vault import PlatformVault


def test_vault_keeps_the_first_value_and_is_private(tmp_path):
    vault = PlatformVault(tmp_path / "vault")
    assert vault.put_once("openbao-init", {"root_token": "a"}) == {"root_token": "a"}
    assert vault.put_once("openbao-init", {"root_token": "b"}) == {"root_token": "a"}  # recovery material wins
    assert stat.S_IMODE((tmp_path / "vault" / "openbao-init.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "vault").stat().st_mode) == 0o700


@pytest.mark.skipif(platform.system() != "Linux", reason="the script runs on Linux nodes (GNU stat)")
def test_host_paths_are_created_once_and_used_ones_stop(tmp_path):
    path = tmp_path / "var-lib-x"
    # The real script sets owners; here the current user's ids keep it runnable without root.
    import os
    spec = [{"path": str(path), "uid": os.getuid(), "gid": os.getgid(), "mode": "0700"}]
    script = host_paths_script(spec)
    assert subprocess.run(["bash", "-c", script]).returncode == 0
    assert subprocess.run(["bash", "-c", script]).returncode == 0  # an empty, matching directory is accepted
    (path / "data").write_text("x")
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert result.returncode == 3 and "host_path_in_use" in result.stderr  # never reuse existing data


def test_openbao_leaf_names_the_service_and_loopback_for_its_own_readiness_probe():
    material = _openbao_tls()
    leaf = x509.load_pem_x509_certificate(material["leaf_crt"].encode())
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "target-openbao.bravo-secrets.svc" in san.get_values_for_type(x509.DNSName)
    assert ipaddress.ip_address("127.0.0.1") in san.get_values_for_type(x509.IPAddress)
    assert json.dumps(material)  # serialisable for the vault


def test_app_metrics_leaf_is_a_client_certificate_of_the_destination_ca():
    ca = _ca("bravo-app-metrics-ca", days=90)
    leaf = x509.load_pem_x509_certificate(_leaf(ca, "demo-chat-target", days=30, client=True)["crt"].encode())
    assert leaf.issuer == x509.load_pem_x509_certificate(ca["ca_crt"].encode()).subject
    usage = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert list(usage) == [ExtendedKeyUsageOID.CLIENT_AUTH]
