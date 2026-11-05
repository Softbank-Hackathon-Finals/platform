"""A token-free intent and provider-bound receipt for one operator-started Actions run."""

import json
import os
from pathlib import Path

from .product_execution_contracts import (
    ExecutionIntent,
    receipt_binding,
    validate_execution_receipt,
    workflow_boundary,
)
from .subscription_product import stopped
from .subscription_product_caller import ActionsProductToken, product_probe_window, product_window


def execute(intent, env, *, token_supplier=None, window=None):
    workflow_boundary(intent, env)  # Before token acquisition or any SSM operation.
    receipt = receipt_binding(intent, env['GITHUB_RUN_ID']) | {
        'status': 502, 'cleanup_confirmed': False, 'result': stopped('PRODUCT_EXECUTION_UNCONFIRMED')}
    try:
        token = (token_supplier or ActionsProductToken(env))()
        body = {'input_ref': intent.input_ref, 'input_digest': intent.input_digest, 'web_identity_token': token}
        factory = window or (product_probe_window if intent.mode == 'probe' else product_window)
        with factory(env['BRAVO_INSTANCE_ID'], intent.runtime_source_sha) as call:
            status, result = call(body)  # Exactly one request, including response loss.
        receipt['cleanup_confirmed'] = True
        candidate = receipt | {'status': status, 'result': result}
        validate_execution_receipt(intent, env['GITHUB_RUN_ID'], candidate)
        return candidate
    except Exception:
        # No token, source, cloud exception, or unconfirmed proposal is retained.
        return receipt


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--preflight', action='store_true')
    args = parser.parse_args(argv)
    try:
        intent = ExecutionIntent.model_validate_json(os.environ['PRODUCT_INTENT'])
        workflow_boundary(intent, os.environ)
        if args.preflight:
            return 0
        receipt = execute(intent, os.environ)
        output = Path('product-result.json')
        with output.open('x') as stream:
            json.dump(receipt, stream, allow_nan=False)
        output.chmod(0o600)
        validate_execution_receipt(intent, os.environ['GITHUB_RUN_ID'], receipt)
        return 0
    except Exception:
        print('Product execution unconfirmed; inspect only this run receipt.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
