"""Deployment factory: explicit configuration, authentication and pre-migrated state DB."""

import os
from pathlib import Path

from bravo_cutover.api import create_app as api_app
from bravo_cutover.runner import Cutover, ThreadRunner
from bravo_cutover.sites import load_config
from bravo_cutover.store import Store


def create_app():
    config = load_config(os.environ["EXECUTOR_CONFIG"])
    registry = config.deployment_registry()
    token = Path(os.environ["EXECUTOR_API_TOKEN_FILE"]).read_text().strip()
    if len(token) < 32:
        raise ValueError("executor authentication token must contain at least 32 characters")
    store = Store(os.environ["EXECUTOR_DB"])
    runner = ThreadRunner(Cutover(store, config), store)
    return api_app(store, runner, registry=registry, api_token=token)
