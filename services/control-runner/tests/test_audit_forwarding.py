from dataclasses import replace

import pytest

from bravo_controls.audit_forwarding import AuditForwardingRegistration, collector_config


def registration():
    return AuditForwardingRegistration(
        "source", "source", "sha256:" + "a" * 64, "source-dev", "source-db-1",
        "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
        "https://audit-receiver.audit.svc:4318",
    )


@pytest.mark.parametrize("change", [
    {"destination_environment": "platform"}, {"registration_digest": "arbitrary"},
    {"namespace": "*"}, {"pod": "../other"}, {"pod_uid": "*"},
    {"node_uid": "not-a-uid"}, {"environment": 'source\\"'},
    {"include_node_audit": 1},
    *[{"endpoint": x} for x in (
        "http://audit.svc", "https://user:pass@audit.example.invalid", "https://audit.svc/?secret=x",
        "https://audit.svc/#part", "https://169.254.169.254", "https://127.0.0.1",
        "https://localhost", "https://x.localhost", "https://0.0.0.0", "https://224.0.0.1",
        "https://[::1]", "https://audit.svc:0", "https://audit.svc:65536",
        "https://audit.svc/path", "https://audit.svc\n", "https://a b.svc",
    )],
])
def test_unregistered_or_unsafe_binding_is_not_rendered(change):
    with pytest.raises(ValueError):
        replace(registration(), **change)


def test_generated_input_is_exact_uid_and_no_untrusted_otlp_listener():
    value = registration()
    config = collector_config(value)
    assert set(config["receivers"]) == {"filelog/cnpg"}
    paths = config["receivers"]["filelog/cnpg"]["include"]
    assert paths == [f"/var/log/pods/source-dev_source-db-1_{value.pod_uid}/postgres/*.log*"]
    assert config["exporters"]["otlp_http/audit"]["tls"]["insecure_skip_verify"] is False
    assert config["exporters"]["otlp_http/audit"]["tls"]["key_file"].endswith("tls.key")


def test_checkpoint_and_queue_persist_and_do_not_expire_retries():
    config = collector_config(registration())
    receiver = config["receivers"]["filelog/cnpg"]
    exporter = config["exporters"]["otlp_http/audit"]
    assert receiver["storage"] == exporter["sending_queue"]["storage"] == "file_storage/audit"
    assert receiver["retry_on_failure"]["max_elapsed_time"] == "0s"
    assert exporter["retry_on_failure"]["max_elapsed_time"] == "0s"
    storage = config["extensions"]["file_storage/audit"]
    assert storage["recreate"] is False and storage["fsync"] is True
    assert storage["max_size"] == 64 * 1024 * 1024


def test_node_audit_opt_in_and_bindings_do_not_mix():
    config = collector_config(replace(registration(), include_node_audit=True))
    assert set(config["receivers"]) == {"filelog/cnpg", "filelog/kubernetes"}
    assert len(config["service"]["pipelines"]) == 2
    assert "transform/binding-cnpg" in config["service"]["pipelines"]["logs/cnpg"]["processors"]
    assert "transform/binding-kubernetes" in config["service"]["pipelines"]["logs/kubernetes"]["processors"]


def test_plain_dictionary_is_not_operator_registration():
    with pytest.raises(ValueError):
        collector_config({"environment": "source"})
