"""Token-free execution intent and receipt contracts shared with T10."""

import re
from typing import Literal

from pydantic import ConfigDict, Field

from .contracts import Contract, Digest, digest
from .proposal_contracts import validate_bound_product_response

REPOSITORY = 'Softbank-Hackathon-Finals/platform'
WORKFLOW = 'subscription-product.yml'
ARTIFACT = 'subscription-product-result'


class ExecutionPending(RuntimeError):
    """The same execution has not produced a confirmed result yet."""


class ExecutionUnknown(RuntimeError):
    """Do not dispatch again; only investigate/read the existing execution."""


class ExecutionIntent(Contract):
    model_config = ConfigDict(revalidate_instances='always')
    request_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$')
    input_ref: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$')
    input_digest: Digest
    admission_digest: Digest
    runtime_source_sha: str = Field(pattern=r'^[a-f0-9]{40}$')
    recipient_public_key: str = Field(pattern=r'^[a-f0-9]{64}$')
    mode: Literal['probe', 'propose']


def workflow_boundary(intent, env):
    expected = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': REPOSITORY,
                'GITHUB_REPOSITORY_ID': '1443045223', 'GITHUB_REPOSITORY_OWNER_ID': '350259663',
                'GITHUB_REF': 'refs/heads/develop', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                'GITHUB_WORKFLOW_REF': f'{REPOSITORY}/.github/workflows/{WORKFLOW}@refs/heads/develop',
                'GITHUB_SHA': intent.runtime_source_sha, 'PRODUCT_WORKFLOW_SHA': intent.runtime_source_sha,
                'GITHUB_RUN_ATTEMPT': '1'}
    if (any(env.get(k) != v for k, v in expected.items())
            or re.fullmatch(r'[1-9][0-9]{0,19}', env.get('GITHUB_RUN_ID', '')) is None):
        raise ValueError('product workflow identity mismatch')


def receipt_binding(intent, run_id):
    return {'schema_version': 1, 'request_id': intent.request_id, 'mode': intent.mode,
            'input_ref_digest': digest(intent.input_ref), 'input_digest': intent.input_digest,
            'admission_digest': intent.admission_digest, 'runtime_source_sha': intent.runtime_source_sha,
            'workflow_sha': intent.runtime_source_sha, 'github_run_id': str(run_id), 'github_run_attempt': 1,
            'recipient_key_digest': digest(intent.recipient_public_key)}


def validate_execution_receipt(intent, run_id, receipt):
    expected = receipt_binding(intent, run_id)
    if (type(receipt) is not dict or set(receipt) != set(expected) | {'status', 'cleanup_confirmed', 'result'}
            or any(type(receipt[k]) is not type(v) or receipt[k] != v for k, v in expected.items())
            or type(receipt['status']) is not int or receipt['status'] != 200
            or receipt['cleanup_confirmed'] is not True):
        raise ValueError('product execution unconfirmed')
    result = receipt['result']
    if intent.mode == 'propose':
        validate_bound_product_response(result, intent.input_digest, intent.runtime_source_sha)
    else:
        probe = {'state': 'INPUT_AUTHENTICATED', 'runtime_source_sha': intent.runtime_source_sha,
                 'product_input_digest': intent.input_digest,
                 'request_digest': digest({'input_ref': intent.input_ref, 'input_digest': intent.input_digest}),
                 'admission_digest': intent.admission_digest}
        if (type(result) is not dict or set(result) != set(probe) | {'principal_digest'}
                or any(result[k] != v for k, v in probe.items())
                or not isinstance(result['principal_digest'], str)
                or re.fullmatch(r'[a-f0-9]{64}', result['principal_digest']) is None):
            raise ValueError('unbound probe receipt')
    if result['admission_digest'] != intent.admission_digest:
        raise ValueError('different admission result')
    return receipt
