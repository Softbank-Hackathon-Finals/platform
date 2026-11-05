"""Deployment factory: explicit configuration, authentication and pre-migrated state DB."""

import os
from pathlib import Path

from bravo_cutover.api import create_app as api_app
from bravo_cutover.api import validate_credentials
from bravo_cutover.runner import Cutover, ThreadRunner
from bravo_cutover.sites import load_config
from bravo_cutover.store import Store


def _read_token(path: str) -> str:
    token = Path(path).read_text().strip()
    if len(token) < 32 or not token.isascii() or not token.isprintable() or " " in token:
        raise ValueError("executor authentication token must contain at least 32 visible ASCII characters")
    return token


def create_app():
    config = load_config(os.environ["EXECUTOR_CONFIG"])
    registry = config.deployment_registry()
    token = _read_token(os.environ["EXECUTOR_API_TOKEN_FILE"])
    command_file = os.environ.get("EXECUTOR_COMMAND_TOKEN_FILE")
    command_token = _read_token(command_file) if command_file is not None else None
    # Validate before ThreadRunner acquires the singleton and recovers outstanding work.
    validate_credentials(token, command_token)
    store = Store(os.environ["EXECUTOR_DB"])
    runner = ThreadRunner(Cutover(store, config), store)
    return api_app(store, runner, registry=registry, api_token=token, command_token=command_token)
