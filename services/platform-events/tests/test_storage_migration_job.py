"""Deployment boundary: explicit migrations never inherit runtime or producer credentials."""
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

DEPLOY = Path(__file__).resolve().parents[1] / 'deploy/window'
spec = importlib.util.spec_from_file_location('storage_jobs', DEPLOY / 'jobs.py')
jobs = importlib.util.module_from_spec(spec)
render_spec = importlib.util.spec_from_file_location('window_render', DEPLOY / 'render.py')
render = importlib.util.module_from_spec(render_spec)
render_spec.loader.exec_module(render)
with patch.dict(sys.modules, {'render': render}):
    spec.loader.exec_module(jobs)
IMAGE = 'bravo-window-intake:source-' + 'a' * 64


@pytest.mark.parametrize('version', [2, 3])
def test_migration_is_bounded_and_uses_separate_role(version):
    manifest, name = jobs.job(IMAGE, 'migrate', schema_version=version)
    config, job = manifest['items']
    assert config['immutable'] is True
    assert config['metadata']['name'] == name == job['metadata']['name']
    assert json.loads(config['data']['expected.json']) == {'schema_version': version}
    assert job['spec']['backoffLimit'] == 0
    assert job['spec']['activeDeadlineSeconds'] == 120
    pod = job['spec']['template']['spec']
    assert pod['automountServiceAccountToken'] is False
    assert {v['secret']['secretName'] for v in pod['volumes'] if 'secret' in v} == {
        'platform-migration-db', 'platform-db-ca'}
    assert not any(v['name'] == 'producer' for v in pod['volumes'])
    assert pod['containers'][0]['imagePullPolicy'] == 'Never'
    assert 'SET lock_timeout' in config['data']['run.py']
    assert 'SET statement_timeout' in config['data']['run.py']


def test_different_migrations_cannot_reuse_immutable_config_name():
    v2, name2 = jobs.job(IMAGE, 'migrate')
    v3, name3 = jobs.job(IMAGE, 'migrate', schema_version=3)
    assert name2 != name3
    assert '0002' not in v3['items'][0]['data']['migration.sql']
    assert 'expected_storage_schema_v2' in v3['items'][0]['data']['migration.sql']
    assert v2['items'][0]['data']['migration.sql'] != v3['items'][0]['data']['migration.sql']


@pytest.mark.parametrize('version', [True, '3', 0, 4])
def test_unreviewed_migration_rejected(version):
    with pytest.raises(ValueError, match='unknown_storage_schema'):
        jobs.job(IMAGE, 'migrate', schema_version=version)


def test_schema_three_never_enables_synthetic_operational_registration():
    with pytest.raises(ValueError, match='synthetic_enrollment_is_not_registration'):
        jobs.job(IMAGE, 'enroll', schema_version=3)
