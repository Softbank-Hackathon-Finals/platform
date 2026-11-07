"""모의 환경의 환경 이름 · 종류 매개변수 (ADR-0034, A9). 클러스터 없이 설정 생성만 본다."""

import argparse
import importlib.util
from pathlib import Path

import pytest

from bravo_cutover.sites import Config

_spec = importlib.util.spec_from_file_location("mockenv", Path(__file__).resolve().parents[1] / "testing" / "mockenv.py")
mockenv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mockenv)

CREDS = {k: "x" for k in ("postgres-password", "chat-app-password", "control-password", "replicator-password")}


def test_default_pair_keeps_the_existing_names():
    config = Config.model_validate(mockenv.executor_config(CREDS, mockenv.make_pair()))
    assert config.origin == "env-aws" and set(config.sites) == {"env-aws", "env-onprem"}
    assert (config.sites["env-aws"].kind, config.sites["env-onprem"].kind) == ("aws", "onprem")
    assert config.sites["env-aws"].connection_ref == "mock/src"


def test_named_pair_is_written_with_kinds_and_same_wiring():
    pair = mockenv.make_pair(mockenv.parse_endpoint("gcp-a:gcp"), mockenv.parse_endpoint("aws-b:aws"))
    config = Config.model_validate(mockenv.executor_config(CREDS, pair))
    default = Config.model_validate(mockenv.executor_config(CREDS, mockenv.make_pair()))
    assert config.origin == "gcp-a" and config.direction("gcp-a", "aws-b") == "cutover"
    assert (config.sites["gcp-a"].kind, config.sites["aws-b"].kind) == ("gcp", "aws")
    # 이름 · 종류 말고는 같은 배선이다(네임스페이스 · 포트 · 접속 참조 · 구독)
    for env, base in (("gcp-a", "env-aws"), ("aws-b", "env-onprem")):
        changed = {"environment_id", "kind"}
        assert config.sites[env].model_dump(exclude=changed) == default.sites[base].model_dump(exclude=changed)
    assert config.deployment_registry().public_pair().target.environment_id == "aws-b"


def test_same_kind_pair_is_two_environments():
    pair = mockenv.make_pair(mockenv.parse_endpoint("aws-a:aws"), mockenv.parse_endpoint("aws-b:aws"))
    config = Config.model_validate(mockenv.executor_config(CREDS, pair))
    assert len({s.resource_id for s in config.sites.values()}) == 2


@pytest.mark.parametrize("value", ["gcp-a", "gcp-a:", ":gcp", "gcp a:gcp", "gcp-a:GCP"])
def test_endpoint_argument_needs_id_and_kind(value):
    with pytest.raises(argparse.ArgumentTypeError):
        mockenv.parse_endpoint(value)


def test_source_and_target_names_must_differ():
    with pytest.raises(ValueError):
        mockenv.make_pair(mockenv.parse_endpoint("x:aws"), mockenv.parse_endpoint("x:gcp"))
