"""TLS service read entry point on the existing schema 2/3 state database."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from jwt.algorithms import RSAAlgorithm
from rehearsal_runner.task_catalog import TaskCatalog

from .executor import _object
from .postgres import PostgresPlanStore
from .product_access import ProductInputAccess
from .product_api import KubernetesJobCaller
from .runtime_probe import connection_checks
from .window_server import runtime_connection
from .workload_identity import WorkloadKeySet

# Reader config v2: the platform Job identity only (the Actions caller is removed).
_FIELDS = {"version", "tenant_id", "catalog_root", "catalog_key_file", "job_identity"}
_JOB_FIELDS = {"issuer", "audience", "subject", "jwks_file"}


def _json(path, limit):
    with Path(path).open() as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("product_config_too_large")
    return json.loads(value, object_pairs_hook=_object)


def _public_keys(jwks):
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if (type(keys) is not list or not 1 <= len(keys) <= 32
            or any(type(key) is not dict or set(key) & {"d", "p", "q", "dp", "dq", "qi", "oth"}
                   or key.get("kty") != "RSA" or key.get("alg", "RS256") != "RS256"
                   or key.get("use", "sig") != "sig" for key in keys)):
        raise ValueError("job_public_keys_required")
    return tuple((key.get("kid"), RSAAlgorithm.from_jwk(json.dumps(key))) for key in keys)


def configured_job_caller(identity, tenant_id):
    """Pinned platform-cluster keys for the Job; reviewed install config, no key URL."""
    if type(identity) is not dict or set(identity) != _JOB_FIELDS or not Path(identity["jwks_file"]).is_absolute():
        raise ValueError("invalid_job_configuration")
    keys = _public_keys(_json(identity["jwks_file"], 65536))
    return KubernetesJobCaller(WorkloadKeySet(identity["issuer"], identity["audience"], identity["subject"],
                                              keys, tenant_id))


def configured_initial_reader(store, config_file):
    """Read reviewed mounts; never create grants, catalogs, keys or credentials."""
    from .initial_proposal import InitialProposalRecords
    from .initial_proposal_api import AuthenticatedInitialReader
    from .initial_stored_source import stored_initial_source

    config = _json(config_file, 16384)
    if type(config) is not dict or set(config) != _FIELDS or config["version"] != 2:
        raise ValueError("invalid_product_configuration")
    paths = {name: Path(config[name]) for name in ("catalog_root", "catalog_key_file")}
    if any(not path.is_absolute() for path in paths.values()) or not paths["catalog_root"].is_dir():
        raise ValueError("product_mounts_required")
    with paths["catalog_key_file"].open("rb") as stream:
        catalog_key = stream.read(4097)
    if not 32 <= len(catalog_key) <= 4096:
        raise ValueError("product_catalog_key_required")

    def clock():
        return datetime.now(UTC).isoformat()
    access = ProductInputAccess(store, clock=clock, purpose="t38-initial-proposal")
    records = InitialProposalRecords(store, catalog=TaskCatalog(paths["catalog_root"], catalog_key),
        source=stored_initial_source, authorize=access.require, clock=clock)
    return AuthenticatedInitialReader(records, configured_job_caller(config["job_identity"], config["tenant_id"]))


def load_initial_app(store, create_reader):
    from .initial_proposal_api import AuthenticatedInitialReader, create_initial_read_app

    with store._transaction() as c:
        if c.execute("SELECT version FROM schema_version").fetchall() not in ([{"version": 2}], [{"version": 3}]):
            raise ValueError("product_read_schema_required")
    reader = create_reader(store)
    if type(reader) is not AuthenticatedInitialReader or reader.records.store is not store:
        raise ValueError("same_store_initial_reader_required")
    return create_initial_read_app(reader)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--tls-cert", required=True)
    parser.add_argument("--tls-key", required=True)
    parser.add_argument("--initial-proposal", action="store_true", required=True,
                        help="Read already admitted initial inputs for the platform Job identity")
    args = parser.parse_args(argv)
    try:
        if not all(Path(p).is_file() for p in (args.config, args.tls_cert, args.tls_key)):
            raise ValueError()
        connection = runtime_connection()
        connection_checks(connection)
        app = load_initial_app(PostgresPlanStore(connection),
                               lambda store: configured_initial_reader(store, args.config))
    except Exception:
        raise SystemExit("product_read_startup_unavailable") from None
    uvicorn.run(app, host="0.0.0.0", port=8443, workers=1,
        ssl_certfile=args.tls_cert, ssl_keyfile=args.tls_key, access_log=False,
        proxy_headers=False, server_header=False, limit_concurrency=16, timeout_keep_alive=5)


if __name__ == "__main__":
    main()
