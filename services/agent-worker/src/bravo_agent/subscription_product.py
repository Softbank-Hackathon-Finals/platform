"""T38 product proposal boundary; verification receipts remain owned by T15.

The host must provide an authenticated source reader. Caller-supplied JSON is
only a reference, never an Admission or an authority to select source files.
"""

import contextlib
import io
import json
import os
import re
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Protocol

from .contracts import FailureBundle, digest
from .product_contracts import ProductInput as ProductInput
from .product_contracts import ProductRequest as ProductRequest
from .product_contracts import validate_product_input
from .subscription_admin import run_worker
from .subscription_store import SubscriptionStore, private_directory
from .tools import Snapshot, Tools
from .verification import validate_result


@dataclass(frozen=True)
class ProductAccess:
    # Stable digest of the STS-validated issuer/audience/subject, never the JWT.
    principal: str
    credentials: dict = field(repr=False)


class ProductInputs(Protocol):
    def resolve(self, principal: str, input_ref: str) -> ProductInput:
        """Authorize principal, read committed Admission and immutable source.

        Check run/source/task/plan/version/tenant/attempt against that Admission,
        plus the current registration and allowed purpose. Unknown, expired or
        uncommitted input must raise. A caller-provided Admission is insufficient.
        """
        ...


class ProposalJournal:
    """Private durable model-call journal, not signed verification evidence.

    Keep this directory across restarts. Loss/rollback requires an operator hold;
    a fresh directory is not permission to repeat previously admitted calls.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        private_directory(self.root)
        self.path = self.root / "proposals.sqlite3"
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        info = self.path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid != os.getuid() or info.st_mode & 0o077):
            raise ValueError("private journal required")
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS proposals "
                       "(key TEXT PRIMARY KEY, binding TEXT NOT NULL, result TEXT, result_digest TEXT)")
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def connect(self):
        with contextlib.closing(sqlite3.connect(self.path, timeout=10)) as db:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db

    def claim(self, key: str, binding: str):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT binding,result,result_digest FROM proposals WHERE key=?", (key,)).fetchone()
            if row is None:
                db.execute("INSERT INTO proposals(key,binding) VALUES(?,?)", (key, binding))
                return True, None
            if row[0] != binding:
                raise ValueError("admitted input changed")
            result = json.loads(row[1]) if row[1] is not None else None
            if result is not None and digest(result) != row[2]:
                raise ValueError("journal result changed")
            return False, result

    def finish(self, key: str, binding: str, result: dict):
        with self.connect() as db:
            changed = db.execute("UPDATE proposals SET result=?,result_digest=? "
                                 "WHERE key=? AND binding=? AND result IS NULL",
                                 (json.dumps(result, allow_nan=False), digest(result), key, binding)).rowcount
            if changed != 1:
                raise ValueError("journal already finished or changed")


def stopped(reason):
    return {"state": "NEEDS_HUMAN", "reason": reason}


def stage_input(value: ProductInput, root: Path) -> Snapshot:
    value = validate_product_input(value)
    snapshot_root = root / "source"
    snapshot_root.mkdir(mode=0o700)
    for name, content in value.files.items():
        data = content.encode()
        path = snapshot_root / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as stream:
            stream.write(data)
        path.chmod(0o400)
    snapshot = Snapshot(snapshot_root, value.manifest)
    for name in value.manifest.files:
        snapshot.read(name)
    return snapshot


def proposal_result(result, value, snapshot, expected):
    bundle, proposal = validate_result(result)
    task = FailureBundle.model_validate(result["task"])
    if (bundle != value.bundle or result["snapshot_digest"] != digest(value.manifest)
            or task != value.bundle or result.get("task_digest") != digest(task)
            or ("input_kind" in result and result["input_kind"] != "product")):
        raise ValueError("worker returned a different input")
    Tools(bundle, snapshot, 1).dispatch("mcp__bravo__plan_propose", proposal.model_dump())
    # Deliberately project bounded contract fields. Raw worker diagnostics and
    # arbitrary provider fields cannot enter the response or durable record.
    return {**expected, "state": "PROPOSAL_READY", "input_kind": "product",
            "run_id": bundle.run_id, "source_sha": bundle.source_sha, "attempt": 1,
            "input": bundle.model_dump(), "input_digest": digest(bundle),
            "task": task.model_dump(), "task_digest": digest(task),
            "snapshot_digest": digest(value.manifest), "proposal": proposal.model_dump(),
            "proposal_digest": digest(proposal), "tool_events_status": "UNAVAILABLE"}


@dataclass
class ProductService:
    journal: ProposalJournal
    authenticate: Callable[[str], ProductAccess] = field(repr=False)
    inputs: ProductInputs = field(repr=False)
    invoke: Callable = field(repr=False)
    runtime_source_sha: str
    request_inputs: Callable[[str], ProductInputs] | None = field(default=None, repr=False)

    def __post_init__(self):
        if re.fullmatch(r"[a-f0-9]{40}", self.runtime_source_sha) is None:
            raise ValueError("exact runtime source required")

    def run(self, body: dict):
        try:
            request = ProductRequest.model_validate(body)
        except ValueError:
            return 400, stopped("INVALID_PRODUCT_REQUEST")
        try:
            access = self.authenticate(request.web_identity_token)
            if not isinstance(access, ProductAccess) or re.fullmatch(r"[a-f0-9]{64}", access.principal) is None:
                raise ValueError("invalid authenticated access")
        except Exception:
            return 403, stopped("PRODUCT_ACCESS_DENIED")
        try:
            # Trusted host factory is request-scoped and runs only after STS authentication.
            # The token is never added to ProductInput, the worker access, or the journal.
            inputs = self.inputs if self.request_inputs is None else self.request_inputs(request.web_identity_token)
            value = inputs.resolve(access.principal, request.input_ref)
            # Revalidate/detach mutable dictionaries even when already typed.
            value = validate_product_input(value)
            if digest(value) != request.input_digest or value.attempt != 1:
                raise ValueError("unbound input or unsupported retry")
            expected = {"product_input_digest": request.input_digest,
                        "admission_digest": value.admission_digest,
                        "runtime_source_sha": self.runtime_source_sha}
            binding = digest({"principal": access.principal, **expected})
            key = f"{value.bundle.run_id}:{value.attempt}"
            with TemporaryDirectory(prefix="product-", dir=self.journal.root) as staging:
                root = Path(staging)
                snapshot = stage_input(value, root)
                fresh, saved = self.journal.claim(key, binding)
                if not fresh:
                    if saved is None:
                        return 200, stopped("PRODUCT_OUTCOME_UNKNOWN")
                    if saved.get("state") == "NEEDS_HUMAN":
                        return 200, stopped("PRODUCT_RUNTIME_UNCONFIRMED")
                    checked = proposal_result(saved, value, snapshot, expected)
                    if checked != saved:
                        raise ValueError("saved result binding changed")
                    return 200, checked
                try:
                    raw = self.invoke(access, value, root, snapshot)
                    response = proposal_result(raw, value, snapshot, expected)
                except Exception:
                    response = stopped("PRODUCT_RUNTIME_UNCONFIRMED")
                self.journal.finish(key, binding, response)
                return 200, response
        except Exception:
            # No exception text: source providers and SDKs may include secrets.
            return 409, stopped("PRODUCT_INPUT_OR_RECORD_UNAVAILABLE")


def invoke_product_worker(settings, access, value, root, snapshot):
    """Use existing isolated T11 CLI. This bridge only supports first proposals."""
    import boto3
    from botocore.config import Config

    credentials = access.credentials
    client = boto3.client("secretsmanager", region_name=settings["region"],
                         config=Config(retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=20),
                         aws_access_key_id=credentials["AccessKeyId"],
                         aws_secret_access_key=credentials["SecretAccessKey"],
                         aws_session_token=credentials["SessionToken"])
    store = SubscriptionStore(settings["secret_arn"], settings["runtime_host_id"], client=client)
    for name, model in (("bundle.json", value.bundle), ("manifest.json", value.manifest)):
        path = root / name
        with path.open("x") as stream:
            stream.write(model.model_dump_json())
        path.chmod(0o400)
    output = root / "result.json"
    args = SimpleNamespace(model=settings["model"], bundle=root / "bundle.json",
                           manifest=root / "manifest.json", snapshot=snapshot.root, output=output)
    with contextlib.redirect_stdout(io.StringIO()):
        code = run_worker(store, Path(settings["state_dir"]), args)
    if code != 0:
        raise ValueError("worker outcome unconfirmed")
    with output.open("rb") as stream:
        raw = stream.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("oversized worker result")
    return json.loads(raw)
