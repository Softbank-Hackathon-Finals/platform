"""Static detection of cloud dependencies in application code and configuration.

Python is parsed with ``ast`` (no regex guessing on call sites). Configuration files
(compose, Kubernetes YAML, Terraform, .env examples) are scanned for environment
variable names, AWS endpoints, ARNs and hard-coded regions. Every finding carries the
file, line and a masked snippet so a reviewer can verify it without the model.
"""

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .catalog import OTHER_CLOUD_PREFIXES, role_for_service

REGION_RE = re.compile(r"\b(?:ap|us|eu|sa|ca|af|me|il|mx)-(?:[a-z]+)-\d\b")
ENDPOINT_RE = re.compile(r"\b[a-z0-9.-]*amazonaws\.com\b")
ARN_RE = re.compile(r"\barn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d{0,12}:[^\s\"']+")
SECRET_LIKE_RE = re.compile(
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|(?i:\b(?:password|secret|token|api[_-]?key)\b\s*[:=]\s*[\"'][^\"']{4,}[\"'])"
)
SECRET_NAME_RE = re.compile(r"(?i)secret|password|passwd|token|_key$|access_key|private|credential|dsn|_url$|_uri$")
USERINFO_RE = re.compile(r"://[^/\s@]+:[^/\s@]*@")
# Credentials carried in a URL query (token, API key, signed URL). The snippet is what the
# console and the model see, so any such line is replaced, not trimmed.
QUERY_SECRET_RE = re.compile(
    r"(?i)[?&](?:access[_-]?token|id[_-]?token|refresh[_-]?token|token|api[_-]?key|apikey|key|secret|"
    r"password|passwd|pwd|sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token|"
    r"x-goog-signature|sv|se|sp|sas)=[^&\s\"']+"
)
# Values copied verbatim into the inventory must look like identifiers (bucket, secret id,
# host). Anything else is replaced by a marker; the model and the console never need the raw value.
SAFE_VALUE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,62}$")
AWS_ENV_RE = re.compile(r"^(?:AWS_|S3_|DB_SECRET|KMS_|SECRETS?_|IRSA_|EKS_)")
TF_RESOURCE_RE = re.compile(r'^\s*resource\s+"(aws_[a-z0-9_]+)"\s+"([A-Za-z0-9_-]+)"')
ENV_LINE_RE = re.compile(r"^\s*(?:-\s*)?([A-Z][A-Z0-9_]{2,})\s*[:=]\s*(.*)$")

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "dist", "build", ".terraform", "tests", "test"}
CONFIG_SUFFIXES = {".yaml", ".yml", ".env", ".example", ".toml", ".ini", ".cfg", ".json"}

MAX_FILE_BYTES = 512 * 1024


@dataclass(frozen=True)
class Finding:
    """One static observation. ``kind`` is a closed set validated by the output schema."""

    id: str
    kind: str  # sdk_call | sdk_import | other_cloud_import | env_ref | region_literal | endpoint_literal | arn_literal | iac_resource | config_env
    role: str
    service: str
    file: str
    line: int
    snippet: str
    detail: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def _mask(text: str) -> str:
    text = text.strip().replace("\n", " ")
    if SECRET_LIKE_RE.search(text) or USERINFO_RE.search(text) or QUERY_SECRET_RE.search(text):
        return "<masked: secret-like literal>"
    return text[:160]


def safe_endpoint(value: str) -> str:
    """Reduce a URL or host literal to ``scheme://host[:port]``. User info, path, query and
    fragment are dropped so a DSN such as ``postgresql://user:password@db/chat`` yields
    ``postgresql://db``. Values that do not parse as a host are replaced by a marker."""
    value = value.strip()
    if "://" in value:
        try:
            parts = urlsplit(value)
        except ValueError:
            return "<masked: unparsable endpoint>"
        if parts.hostname:
            port = f":{parts.port}" if parts.port else ""
            return f"{parts.scheme}://{parts.hostname}{port}"
        return "<masked: unparsable endpoint>"
    if "@" in value:
        return "<masked: endpoint with user info>"
    if re.fullmatch(r"[A-Za-z0-9.-]+(?::\d{1,5})?", value):
        return value[:120]
    return "<masked: not a host literal>"


def safe_value(name: str, value: str) -> str | None:
    """Return a configuration value only if its name is not secret-like and the value is an
    identifier (bucket, secret id, host). Otherwise ``None``: the name alone is recorded."""
    if SECRET_NAME_RE.search(name) or not value:
        return None
    value = value.strip().strip("\"'")
    return value if SAFE_VALUE_RE.fullmatch(value) else None


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _iter_files(root: Path, suffixes: set[str]) -> list[Path]:
    out: list[Path] = []
    for path in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix.lower() in suffixes or path.name in {".env.example", "Dockerfile"}:
            if path.stat().st_size <= MAX_FILE_BYTES:
                out.append(path)
    return out


class _PyVisitor(ast.NodeVisitor):
    def __init__(self, rel: str, lines: list[str], counter: list[int]):
        self.rel, self.lines, self.counter = rel, lines, counter
        self.findings: list[Finding] = []
        self.boto_aliases: set[str] = set()

    def _new(self, kind: str, role: str, service: str, node: ast.AST, **detail: str) -> None:
        self.counter[0] += 1
        line = getattr(node, "lineno", 0)
        snippet = self.lines[line - 1] if 0 < line <= len(self.lines) else ""
        self.findings.append(
            Finding(f"dep-{self.counter[0]:04d}", kind, role, service, self.rel, line, _mask(snippet), detail)
        )

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.name
            if name.split(".")[0] in {"boto3", "botocore", "aiobotocore", "aioboto3"}:
                self.boto_aliases.add(alias.asname or name.split(".")[0])
                self._new("sdk_import", "unknown", name.split(".")[0], node)
            for prefix, cloud in OTHER_CLOUD_PREFIXES:
                if name.startswith(prefix):
                    self._new("other_cloud_import", "unknown", name, node, cloud=cloud)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        if mod.split(".")[0] in {"boto3", "botocore"}:
            self._new("sdk_import", "unknown", mod, node)
        for prefix, cloud in OTHER_CLOUD_PREFIXES:
            if mod.startswith(prefix):
                self._new("other_cloud_import", "unknown", mod, node, cloud=cloud)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in {"client", "resource"}:
            base = func.value
            base_name = base.id if isinstance(base, ast.Name) else (base.attr if isinstance(base, ast.Attribute) else "")
            if base_name in self.boto_aliases or base_name in {"boto3", "session", "Session"}:
                service = ""
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    service = node.args[0].value
                detail = {"factory": f"{base_name}.{func.attr}"}
                for kw in node.keywords:
                    if kw.arg == "endpoint_url":
                        detail["endpoint_url"] = "configurable" if not isinstance(kw.value, ast.Constant) else safe_endpoint(str(kw.value.value))
                    if kw.arg == "region_name" and isinstance(kw.value, ast.Constant):
                        detail["region_literal"] = str(kw.value.value)
                self._new("sdk_call", role_for_service(service) if service else "unknown", service or "unknown", node, **detail)
        if isinstance(func, ast.Attribute) and func.attr in {"get", "getenv"} or (
            isinstance(func, ast.Name) and func.id == "getenv"
        ):
            self._env_ref(node, node.args[0] if node.args else None, "os.environ.get")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        value = node.value
        is_env = (isinstance(value, ast.Attribute) and value.attr == "environ") or (
            isinstance(value, ast.Name) and value.id in {"env", "environ"}
        )
        if is_env:
            self._env_ref(node, node.slice, "os.environ[]")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            v = node.value
            if ENDPOINT_RE.search(v):
                self._new("endpoint_literal", "unknown", "aws", node, value=safe_endpoint(v))
            elif ARN_RE.search(v):
                self._new("arn_literal", "unknown", "aws", node, value=v[:120])
            elif REGION_RE.fullmatch(v):
                self._new("region_literal", "unknown", "aws", node, value=v)

    def _env_ref(self, node: ast.AST, key: ast.AST | None, how: str) -> None:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            name = key.value
            role = "unknown"
            if name.startswith("S3_"):
                role = "object_store"
            elif name.startswith("DB_SECRET") or name.startswith("SECRET"):
                role = "secrets"
            elif name.startswith("DB_"):
                role = "db"
            elif name.startswith("KMS_"):
                role = "kms"
            elif name.startswith("AWS_"):
                role = "iam"
            self._new("env_ref", role, "env", node, name=name, how=how)


def scan_python(root: Path, app_path: str = ".") -> list[Finding]:
    counter = [0]
    out: list[Finding] = []
    base = (root / app_path).resolve()
    for path in _iter_files(base, {".py"}):
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            counter[0] += 1
            out.append(Finding(f"dep-{counter[0]:04d}", "parse_error", "unknown", "python", _rel(root, path), 0, "", {}))
            continue
        visitor = _PyVisitor(_rel(root, path), text.splitlines(), counter)
        visitor.visit(tree)
        out.extend(visitor.findings)
    return out


def scan_config(root: Path, start_index: int) -> list[Finding]:
    """Compose / Kubernetes YAML / env examples / Terraform. Values are masked, names kept."""
    counter = [start_index]
    out: list[Finding] = []
    for path in _iter_files(root, CONFIG_SUFFIXES | {".tf"}):
        rel = _rel(root, path)
        if path.name == "uv.lock" or path.name == "pnpm-lock.yaml":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = TF_RESOURCE_RE.match(line)
            if m:
                counter[0] += 1
                rtype = m.group(1)
                service = rtype.removeprefix("aws_").split("_")[0]
                out.append(Finding(f"dep-{counter[0]:04d}", "iac_resource", role_for_service(service), rtype, rel, lineno, _mask(line), {"name": m.group(2)}))
                continue
            m = ENV_LINE_RE.match(line)
            if m and AWS_ENV_RE.match(m.group(1)):
                counter[0] += 1
                name, value = m.group(1), m.group(2)
                role = "object_store" if name.startswith("S3_") else "secrets" if "SECRET" in name else "iam"
                detail = {"name": name}
                secret_name = SECRET_NAME_RE.search(name) is not None
                if ENDPOINT_RE.search(value):
                    detail["endpoint"] = "amazonaws.com"
                elif "://" in value:
                    detail["endpoint"] = safe_endpoint(value)
                elif (safe := safe_value(name, value)) is not None:
                    detail["value"] = safe
                masked = secret_name or (bool(value.strip()) and "value" not in detail and "endpoint" not in detail)
                snippet = f"{name}: <masked>" if masked else _mask(line)
                out.append(Finding(f"dep-{counter[0]:04d}", "config_env", role, "env", rel, lineno, snippet, detail))
                continue
            if ARN_RE.search(line):
                counter[0] += 1
                out.append(Finding(f"dep-{counter[0]:04d}", "arn_literal", "unknown", "aws", rel, lineno, _mask(line), {}))
    return out


def scan_repo(root: Path, app_path: str = ".") -> list[Finding]:
    root = root.resolve()
    py = scan_python(root, app_path)
    return py + scan_config(root, len(py))
