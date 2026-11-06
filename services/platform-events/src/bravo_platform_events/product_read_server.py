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
from .product_api import (
    AUDIENCE,
    ISSUER,
    SUBJECT,
    AuthenticatedProductReader,
    GitHubProductCaller,
    KubernetesJobCaller,
    create_product_read_app,
)
from .product_inputs import ProductInputRecords
from .runtime_probe import connection_checks
from .window_server import runtime_connection
from .workload_identity import WorkloadKeySet, WorkloadSender

_FIELDS = {"version", "tenant_id", "sts_provider", "github_public_jwks_file", "github_key_id",
           "catalog_root", "catalog_key_file"}


def _json(path, limit):
    with Path(path).open() as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise ValueError("product_config_too_large")
    return json.loads(value, object_pairs_hook=_object)


def configured_reader(store, config_file):
    """Read reviewed mounts; never create grants, catalogs, keys or credentials."""
    config = _json(config_file, 16384)
    if (type(config) is not dict or set(config) not in (_FIELDS, _FIELDS - {"github_key_id"})
            or type(config["version"]) is not int
            or config["version"] != 1):
        raise ValueError("invalid_product_configuration")
    paths = {name: Path(config[name]) for name in ("github_public_jwks_file", "catalog_root", "catalog_key_file")}
    if any(not path.is_absolute() for path in paths.values()) or not paths["catalog_root"].is_dir():
        raise ValueError("product_mounts_required")
    jwks = _json(paths["github_public_jwks_file"], 65536)
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if (type(keys) is not list or not 1 <= len(keys) <= 32
            or any(type(key) is not dict or set(key) & {"d", "p", "q", "dp", "dq", "qi", "oth"} for key in keys)):
        raise ValueError("product_public_keys_required")
    if "github_key_id" in config:
        selected = [key for key in keys if key.get("kid") == config["github_key_id"]]
        if not isinstance(config["github_key_id"], str) or not config["github_key_id"] or len(selected) != 1:
            raise ValueError("product_public_key_required")
    else:
        selected = keys
    if any(key.get("kty") != "RSA" or key.get("alg", "RS256") != "RS256"
           or key.get("use", "sig") != "sig" or key.get("key_ops", ["verify"]) != ["verify"] for key in selected):
        raise ValueError("product_public_key_required")
    public_keys = tuple((key.get("kid"), RSAAlgorithm.from_jwk(json.dumps(key))) for key in selected)
    if "github_key_id" in config:
        sender = WorkloadSender(ISSUER, AUDIENCE, SUBJECT, public_keys[0][1], config["tenant_id"])
    else:
        sender = WorkloadKeySet(ISSUER, AUDIENCE, SUBJECT, public_keys, config["tenant_id"])
    caller = GitHubProductCaller(sender, config["sts_provider"])
    with paths["catalog_key_file"].open("rb") as stream:
        catalog_key = stream.read(4097)
    if not 32 <= len(catalog_key) <= 4096:
        raise ValueError("product_catalog_key_required")
    access = ProductInputAccess(store, clock=lambda: datetime.now(UTC).isoformat())
    records = ProductInputRecords(store, catalog=TaskCatalog(paths["catalog_root"], catalog_key),
                                  authorize=access.require)  # No source producer on this service.
    return AuthenticatedProductReader(records, caller)


def load_app(store, create_reader):
    with store._transaction() as c:
        if c.execute("SELECT version FROM schema_version").fetchall() not in ([{"version": 2}], [{"version": 3}]):
            raise ValueError("product_read_schema_required")
    reader = create_reader(store)
    if type(reader) is not AuthenticatedProductReader or reader.records.store is not store:
        raise ValueError("same_store_product_reader_required")
    return create_product_read_app(reader)


def _public_keys(jwks):
    keys = jwks.get("keys") if isinstance(jwks, dict) else None
    if (type(keys) is not list or not 1 <= len(keys) <= 32
            or any(type(key) is not dict or set(key) & {"d", "p", "q", "dp", "dq", "qi", "oth"}
                   or key.get("kty") != "RSA" or key.get("alg", "RS256") != "RS256"
                   or key.get("use", "sig") != "sig" for key in keys)):
        raise ValueError("job_public_keys_required")
    return tuple((key.get("kid"), RSAAlgorithm.from_jwk(json.dumps(key))) for key in keys)


def configured_job_caller(job_config_file, tenant_id):
    """Pinned platform-cluster keys for the initial Job; reviewed install config, no key URL."""
    config = _json(job_config_file, 16384)
    if (type(config) is not dict or set(config) != {"version", "issuer", "audience", "subject", "jwks_file"}
            or config["version"] != 1 or not Path(config["jwks_file"]).is_absolute()):
        raise ValueError("invalid_job_configuration")
    keys = _public_keys(_json(config["jwks_file"], 65536))
    return KubernetesJobCaller(WorkloadKeySet(config["issuer"], config["audience"], config["subject"],
                                              keys, tenant_id))


def configured_initial_reader(store, config_file, job_config_file=None):
    from .initial_proposal import InitialProposalRecords
    from .initial_proposal_api import AuthenticatedInitialReader
    from .initial_stored_source import stored_initial_source

    base = configured_reader(store, config_file)
    def clock():
        return datetime.now(UTC).isoformat()
    access = ProductInputAccess(store, clock=clock, purpose="t38-initial-proposal")
    records = InitialProposalRecords(store, catalog=base.records.catalog,
        source=stored_initial_source, authorize=access.require, clock=clock)
    caller = base.caller if job_config_file is None else configured_job_caller(
        job_config_file, base.caller.sender.tenant_id)
    return AuthenticatedInitialReader(records, caller)


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
    parser.add_argument("--initial-proposal", action="store_true",
                        help="Read already admitted initial inputs with their separate purpose grant")
    parser.add_argument("--initial-job-config",
                        help="Accept the platform initial Job identity instead of the Actions caller")
    args = parser.parse_args(argv)
    try:
        if not all(Path(p).is_file() for p in (args.config, args.tls_cert, args.tls_key)):
            raise ValueError()
        connection = runtime_connection()
        connection_checks(connection)
        if args.initial_job_config and not args.initial_proposal:
            raise ValueError()
        if args.initial_proposal:
            loader = load_initial_app
            def configure(store, config):
                return configured_initial_reader(store, config, args.initial_job_config)
        else:
            loader, configure = load_app, configured_reader
        app = loader(PostgresPlanStore(connection), lambda store: configure(store, args.config))
    except Exception:
        raise SystemExit("product_read_startup_unavailable") from None
    uvicorn.run(app, host="0.0.0.0", port=8443, workers=1,
        ssl_certfile=args.tls_cert, ssl_keyfile=args.tls_key, access_log=False,
        proxy_headers=False, server_header=False, limit_concurrency=16, timeout_keep_alive=5)


if __name__ == "__main__":
    main()
