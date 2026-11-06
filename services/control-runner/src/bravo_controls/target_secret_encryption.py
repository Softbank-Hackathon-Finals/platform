"""ENC-02.1 target metadata qualification: RKE2 status and OpenBao seal.

Only trusted host composition may provide registration and node command results.
No secret values, encryption configuration file, unseal key, token, rotation,
restart or write API is used. This checks enabled metadata, not every stored byte.
"""

import hashlib
import http.client
import json
import re
import ssl
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from .openbao import _invalid_number, _object
from .postgres_tls import _execute
from .runner import Environment, Observation, RunContext
from .secret_encryption import SPEC, digest, encoded, utc
from .tool_reports import ToolRun

RKE2_VERSION = "v1.35.9+rke2r1"
LIMIT = 16384
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,255}\Z")
VERSION = re.compile(r"2\.7\.2(?:\+[A-Za-z0-9.-]+)?\Z")


@dataclass(frozen=True)
class TargetEncryptionRegistration:
    app: str
    environment: Environment
    cluster_uid: str
    node_uids: tuple[str, ...]
    bao_cluster_id: str
    bao_version: str
    bao_storage: str
    bao_workload_uid: str
    bao_volume_uid: str
    app_secret_uid: str
    delivery_revision: str

    def __post_init__(self):
        identifiers = (self.app, self.cluster_uid, self.bao_workload_uid,
                       self.bao_volume_uid, self.app_secret_uid, self.delivery_revision)
        if (not isinstance(self.environment, Environment) or self.environment.role != "target"
                or any(not isinstance(x, str) or not ID.fullmatch(x) for x in identifiers)
                or type(self.node_uids) is not tuple or not 1 <= len(self.node_uids) <= 5
                or len(set(self.node_uids)) != len(self.node_uids)
                or any(not isinstance(x, str) or not ID.fullmatch(x) for x in self.node_uids)
                or not isinstance(self.bao_version, str) or not VERSION.fullmatch(self.bao_version)
                or self.bao_storage not in {"file", "raft", "pebbledb"}):
            raise ValueError("registered target storage scope required")
        try:
            if str(UUID(self.bao_cluster_id)) != self.bao_cluster_id:
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise ValueError("registered OpenBao cluster required") from None

    def binding(self):
        return asdict(self)


def _load(raw):
    if type(raw) is not bytes or not 0 < len(raw) <= LIMIT:
        raise ValueError("bounded metadata required")
    try:
        result = json.loads(raw, object_pairs_hook=_object, parse_constant=_invalid_number)
        if type(result) is not dict:
            raise ValueError
        return result
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("invalid metadata JSON") from None


class LocalRKE2StatusReader:
    """Run on the registered server, behind an authenticated host/agent boundary.

The host must bind this process to the node UID. Remote execution is not inferred
from a command output string. The pinned binary is rehashed before each read.
"""

    def __init__(self, binary, *, binary_sha256, node_uid):
        path = Path(binary)
        if (not path.is_absolute() or not isinstance(binary_sha256, str)
                or not re.fullmatch(r"[a-f0-9]{64}", binary_sha256)
                or not isinstance(node_uid, str) or not ID.fullmatch(node_uid)):
            raise ValueError("pinned RKE2 binary and registered node required")
        self.binary, self.sha256, self.node_uid = path, binary_sha256, node_uid

    def __call__(self, context, target, node_uid):
        if node_uid != self.node_uid or node_uid not in target.node_uids:
            raise ValueError("node execution mismatch")
        # No caller-supplied token/server/data-dir arguments or inherited env.
        # The fixed RKE2 command reads the local server token internally.
        with self.binary.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != self.sha256:
                raise ValueError("RKE2 executable changed")
        env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
               "LANG": "C", "LC_ALL": "C", "HOME": "/nonexistent"}
        code, output, error = _execute([str(self.binary), "--version"], env=env, cwd="/", timeout=3)
        lines = output.decode("utf-8", errors="replace").splitlines()
        if (code != 0 or error or not lines
                or not re.fullmatch(r"rke2 version " + re.escape(RKE2_VERSION) + r"(?: \([a-f0-9]+\))?", lines[0])):
            raise ValueError("qualified RKE2 version required")
        code, output, error = _execute(
            [str(self.binary), "secrets-encrypt", "status", "--output", "json"],
            env=env, cwd="/", timeout=5,
        )
        if error:
            raise OSError("RKE2 status diagnostic present")
        return ToolRun("rke2-secrets-encrypt", RKE2_VERSION, context.run_id,
                       node_uid, output, code, True)


@dataclass(frozen=True, repr=False)
class OpenBaoSealReader:
    origin: str
    ca_pem: str = field(repr=False)
    timeout: float = 3.0

    def __post_init__(self):
        try:
            url = urlsplit(self.origin)
            if (url.scheme != "https" or not url.hostname or url.username or url.password
                    or url.path or url.query or url.fragment
                    or not re.fullmatch(r"[A-Za-z0-9.:-]+", url.hostname)
                    or (url.port is not None and not 1 <= url.port <= 65535)
                    or type(self.timeout) not in (int, float) or not 0 < self.timeout <= 5):
                raise ValueError
            self._tls()
        except Exception:
            raise ValueError("registered HTTPS origin and CA required") from None

    def _tls(self):
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls.minimum_version = ssl.TLSVersion.TLSv1_2
        tls.load_verify_locations(cadata=self.ca_pem)
        return tls

    def __call__(self):
        url = urlsplit(self.origin)
        connection = http.client.HTTPSConnection(
            url.hostname, url.port or 443, timeout=self.timeout, context=self._tls()
        )
        try:
            # This endpoint needs no token. No redirects, proxies, cookies or retries.
            connection.request("GET", "/v1/sys/seal-status", headers={"Accept": "application/json"})
            response = connection.getresponse()
            if response.status == 403:
                raise PermissionError
            if (response.status != 200
                    or response.getheader("Content-Encoding", "identity") != "identity"
                    or response.getheader("Content-Type", "").split(";", 1)[0].strip() != "application/json"):
                raise ValueError
            return _load(response.read(LIMIT + 1))
        except PermissionError:
            raise PermissionError("OpenBao seal observation denied") from None
        except TimeoutError:
            raise TimeoutError("OpenBao seal observation timed out") from None
        except Exception:
            raise ValueError("OpenBao seal observation unavailable") from None
        finally:
            connection.close()


def _rke2(run, context, node_uid):
    if (not isinstance(run, ToolRun) or run.tool != "rke2-secrets-encrypt"
            or run.version != RKE2_VERSION or run.scope != context.run_id or run.target != node_uid
            or run.complete is not True or type(run.exit_code) is not int or run.exit_code != 0):
        raise ValueError("complete current RKE2 status required")
    value = _load(run.output)
    allowed = {"enable", "stage", "activekey", "hashmatch", "hasherror", "inactivekeys"}
    if set(value) - allowed:
        raise ValueError("unqualified RKE2 status fields")
    # Missing enable (no configuration) is not confused with verified disabled.
    if type(value.get("enable")) is not bool:
        raise ValueError("RKE2 encryption setting unavailable")
    if value.get("hashmatch") is not True or value.get("hasherror"):
        raise ValueError("control-plane encryption state differs")
    if value.get("stage") not in {"start", "reencrypt_finished"}:
        raise ValueError("RKE2 encryption transition in progress")
    if value["enable"]:
        active = value.get("activekey")
        if (not isinstance(active, str)
                or not re.fullmatch(r"(?:AES-CBC|XSalsa20-POLY1305) [A-Za-z0-9_.:+-]{1,255}", active)):
            raise ValueError("active encryption provider unconfirmed")
    if ("inactivekeys" in value and (type(value["inactivekeys"]) is not list
            or len(value["inactivekeys"]) > 32
            or any(type(x) is not str or len(x) > 300 for x in value["inactivekeys"]))):
        raise ValueError("invalid encryption key metadata")
    return value


def _bao(value, target):
    if (type(value) is not dict or value.get("initialized") is not True
            or value.get("sealed") is not False or value.get("migration") is not False
            or value.get("cluster_id") != target.bao_cluster_id
            or value.get("version") != target.bao_version
            or value.get("storage_type") != target.bao_storage
            or value.get("type") != "shamir" or value.get("recovery_seal") is not False
            or type(value.get("t")) is not int or type(value.get("n")) is not int
            or not 1 <= value["t"] <= value["n"] <= 255
            or type(value.get("progress")) is not int or value["progress"] != 0):
        # A sealed vault is not plaintext. It is unavailable/unconfirmed here.
        raise ValueError("registered initialized OpenBao barrier unavailable")
    return {key: value[key] for key in ("cluster_id", "version", "storage_type", "type", "t", "n")}


class TargetSecretEncryptionProbe:
    def __init__(self, target, *, read_registration, read_node_status, read_bao_status,
                 evidence_sink, clock=lambda: datetime.now(UTC), monotonic=time.monotonic):
        if (not isinstance(target, TargetEncryptionRegistration)
                or not all(callable(x) for x in (read_registration, read_node_status,
                                                  read_bao_status, evidence_sink, clock, monotonic))):
            raise ValueError("trusted target readers and durable sink required")
        self.target, self.registration = target, read_registration
        self.nodes, self.bao, self.sink = read_node_status, read_bao_status, evidence_sink
        self.clock, self.monotonic = clock, monotonic

    def __call__(self, context, spec):
        target = self.target
        if (not isinstance(context, RunContext) or context.app != target.app
                or context.environment != target.environment or spec != SPEC
                or not isinstance(context.run_id, str) or not ID.fullmatch(context.run_id)):
            raise ValueError("target encryption scope mismatch")
        started, mono = utc(self.clock()), self.monotonic()
        observed, reason, facts = None, None, {}

        def check():
            if not 0 <= self.monotonic() - mono < 60:
                raise TimeoutError

        def registration():
            check()
            if self.registration(context, target) != target.binding():
                raise ValueError("target registration drift")
            check()

        def reading():
            rows = []
            for uid in target.node_uids:
                check()
                rows.append(_rke2(self.nodes(context, target, uid), context, uid))
                check()
            bao = _bao(self.bao(), target)
            check()
            return rows, bao

        try:
            registration()
            before, after = reading(), reading()
            registration()
            finished = utc(self.clock())
            if before != after or not 0 <= (finished - started).total_seconds() < 60:
                raise ValueError("target metadata changed")
            rows, _ = before
            # All registered control-plane nodes must agree on key/stage/state.
            if any(row != rows[0] for row in rows):
                raise ValueError("registered node metadata differs")
            observed = "enabled" if rows[0]["enable"] else "disabled"
            facts = {"all_registered_nodes_checked": True, "node_count": len(rows),
                     "rke2_encryption_enabled": rows[0]["enable"], "rotation_stable": True,
                     "bao_initialized_unsealed": True, "bao_persistent_storage": True,
                     "registration_unchanged": True, "metadata_only": True}
        except Exception as error:
            reason = ("TIMEOUT" if isinstance(error, TimeoutError)
                      else "OBSERVATION_DENIED" if isinstance(error, PermissionError)
                      else "UNEXPECTED_RESPONSE" if isinstance(error, ValueError) else "TOOL_ERROR")
        finished = utc(self.clock())
        payload = encoded({"format_version": "1", "test_id": SPEC.test_id, "method_version": "1",
                           "run_id": context.run_id, "app": context.app,
                           "environment": asdict(context.environment),
                           "binding_sha256": digest(encoded(target.binding())),
                           "started_at": started.isoformat(), "collected_at": finished.isoformat(),
                           "observed": observed, "unknown_reason": reason, "facts": facts})
        self.sink(payload)
        return Observation(context, SPEC.test_id, "1", observed, "not_applicable", finished,
                           "bravo-target-secret-encryption", "1", digest(payload), reason)
