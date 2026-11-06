"""Explicit initial-purpose VM host; no Admission, input registration or fixture."""

import json
from pathlib import Path

from bravo_agent.subscription_product import ProposalJournal, invoke_product_worker
from bravo_agent.subscription_product_reader import ProductReaderConfig
from bravo_agent.subscription_product_tls import server_context
from bravo_agent.subscription_server import authenticate_caller, serve

from .initial_proposal_runtime import InitialInputHTTPReader, InitialProposalService


def build_service(settings, config):
    config.tls_context()
    return InitialProposalService(
        ProposalJournal(Path(settings["state_dir"]).parent / "initial-proposal-journal"),
        lambda token: authenticate_caller(settings, token),
        lambda token: InitialInputHTTPReader(config, token),
        lambda *args: invoke_product_worker(settings, *args), settings["source_sha"])


def main(settings_path, reader_path):
    settings = json.loads(Path(settings_path).read_text())
    config = ProductReaderConfig(**json.loads(Path(reader_path).read_text()))
    tls = server_context()
    service = build_service(settings, config)
    serve(settings, product=service, fixture_enabled=False, tls_context=tls)


if __name__ == "__main__":
    import sys
    try:
        if len(sys.argv) != 2:
            raise ValueError()
        main(sys.argv[1], "/etc/bravo-subscription/product-reader.json")
    except Exception:
        raise SystemExit("initial host startup unavailable") from None
