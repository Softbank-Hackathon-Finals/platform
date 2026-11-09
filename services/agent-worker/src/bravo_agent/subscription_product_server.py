"""Explicit product host. No input registration, token mount, or fixture execution."""

import json
from pathlib import Path

from .subscription_product_host import RegisteredProductInputs
from .subscription_product_reader import ProductInputHTTPReader, ProductReaderConfig
from .subscription_product_tls import server_context
from .subscription_server import product_service, serve


def build_service(settings, config: ProductReaderConfig):
    def request_inputs(token):
        return RegisteredProductInputs(ProductInputHTTPReader(config, token))

    journal = Path(settings["state_dir"]).parent / "product-journal"
    return product_service(settings, None, journal, request_inputs=request_inputs)


def main(settings_path, reader_path):
    settings = json.loads(Path(settings_path).read_text())
    config = ProductReaderConfig(**json.loads(Path(reader_path).read_text()))
    config.tls_context()  # A missing/invalid mounted CA must stop startup.
    tls = server_context()  # Missing/mismatched credentials close startup.
    product = build_service(settings, config)
    serve(settings, product=product, fixture_enabled=False, tls_context=tls)


if __name__ == "__main__":
    import sys

    try:
        if len(sys.argv) != 2:
            raise ValueError("settings path required")
        main(sys.argv[1], "/etc/bravo-subscription/product-reader.json")
    except Exception:
        raise SystemExit("product host startup unavailable") from None
