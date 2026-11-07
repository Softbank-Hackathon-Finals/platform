"""Static detection of cloud dependencies in application code and configuration.

Python is parsed with ``ast`` (no regex guessing on call sites). Configuration files
(compose, Kubernetes YAML, Terraform, .env examples) are scanned for environment
variable names, AWS endpoints, ARNs and hard-coded regions. Every finding carries the
file, line and a masked snippet so a reviewer can verify it without the model.
"""

from __future__ import annotations

import ast
import copy
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

from .catalog import (
    ENDPOINT_ENV_PREFIX,
    OTHER_CLOUD_PREFIXES,
    SCOPE_FEATURES,
    role_for_service,
    service_for_endpoint_env,
)

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

# `.github`: CI pipeline configuration is not the app's runtime configuration.
SKIP_DIRS = {".git", ".github", ".venv", "node_modules", "__pycache__", "dist", "build", ".terraform", "tests", "test"}
CONFIG_SUFFIXES = {".yaml", ".yml", ".env", ".example", ".toml", ".ini", ".cfg", ".json"}
LOCK_FILES = {"uv.lock", "pnpm-lock.yaml"}
COMPOSE_FILE_RE = re.compile(r"^(?:docker-)?compose(?:\.[\w-]+)?\.ya?ml$")
# Local AWS emulators and the CLI containers that seed them: their env configures the emulator,
# not the app (the app's own compose env stays in scope).
EMULATOR_IMAGE_RE = re.compile(r"(?:^|/)(?:moto|motoserver|localstack|aws-cli)(?:[:@]|$)")
# Features outside the T46 compatible runtime (catalog.SCOPE_FEATURES), as they appear in
# configuration (Terraform, CloudFormation/SAM, compose) and in Python.
FIFO_NAME_RE = re.compile(r"[A-Za-z0-9_-]\.fifo\b")
CONFIG_SCOPE_RES = (
    (re.compile(r"[A-Za-z0-9_-]\.fifo\b|(?i:\bfifo_?queue\b)[\"']?\s*[:=]\s*[\"']?true"), "sqs_fifo"),
    (re.compile(r"\bReportBatchItemFailures\b"), "batch_item_failures"),
    (re.compile(r"\bFilterCriteria\b"), "event_filter"),
    (re.compile(r"\bStartingPosition\b"), "non_sqs_source"),
)
# Inside a Terraform ``aws_lambda_event_source_mapping`` block only.
TF_MAPPING_SCOPE_RES = (
    (re.compile(r"^\s*filter_criteria\b"), "event_filter"),
    (re.compile(
        r"^\s*(?:starting_position|self_managed_event_source|amazon_managed_kafka_event_source_config|"
        r"self_managed_kafka_event_source_config|document_db_event_source_config|queues|topics)\b"
    ), "non_sqs_source"),
)
PY_FIFO_KWARGS = {"MessageGroupId", "MessageDeduplicationId"}
PY_SCOPE_CONSTANTS = {"FifoQueue": "sqs_fifo", "batchItemFailures": "batch_item_failures", "ReportBatchItemFailures": "batch_item_failures"}

MAX_FILE_BYTES = 512 * 1024


@dataclass(frozen=True)
class Finding:
    """One static observation. ``kind`` is a closed set validated by the output schema."""

    id: str
    kind: str  # sdk_call | sdk_import | other_cloud_import | env_ref | region_literal | endpoint_literal | arn_literal | iac_resource | config_env | unsupported_feature
    role: str
    service: str
    file: str
    line: int
    snippet: str
    detail: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,15}://[^\s<>]+")


def _secret_like(text: str) -> bool:
    """Known credential shapes, checked on the text and on its percent-decoded form
    (``%61ccess_token`` is ``access_token``)."""
    for candidate in (text, unquote(text)):
        if SECRET_LIKE_RE.search(candidate) or USERINFO_RE.search(candidate) or QUERY_SECRET_RE.search(candidate):
            return True
    return False


def _url_data_parts(url: str) -> list[str]:
    """The parts of a URL that never belong in an inventory: query, fragment, user info."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return [url]
    pieces: list[str] = []
    for component in (parts.query, parts.fragment):
        pieces.extend(re.split(r"[&=;]", component or ""))
    pieces.extend(x for x in (parts.username, parts.password) if x)
    return [x for x in pieces if x]


def redact_url_text(url: str) -> str:
    """Keep only what the inventory needs from a URL: scheme, host, port and path. Query,
    fragment and user info are dropped wholesale; no name list decides what is sensitive."""
    trailing = ""
    while url and url[-1] in "\"')],;":
        trailing, url = url[-1] + trailing, url[:-1]
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<url-removed>" + trailing
    if not parts.hostname:
        return "<url-removed>" + trailing
    port = f":{parts.port}" if parts.port else ""
    out = f"{parts.scheme}://{parts.hostname}{port}{parts.path}"
    if parts.query:
        out += "?<query-removed>"
    if parts.fragment:
        out += "#<fragment-removed>"
    return out + trailing


def _redact_url(match: re.Match) -> str:
    return redact_url_text(match.group(0))


def _redact_literal(value: str) -> str:
    """A *complete* string constant, redacted as one unit (never cut at quotes or parentheses)."""
    if _secret_like(value):
        return "<masked>"
    if "://" in value:
        return redact_url_text(value.strip())
    return value if len(value) <= 80 else value[:77] + "..."


def node_snippet(node: ast.AST) -> str:
    """The finding's own node, re-rendered with every string constant redacted. The snippet
    therefore covers exactly what was inspected (not the rest of the source line) and a string
    is always handled whole, however it was escaped in the source."""
    clone = copy.deepcopy(node)
    for child in ast.walk(clone):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            child.value = _redact_literal(child.value)
    try:
        text = ast.unparse(clone)
    except Exception:  # noqa: BLE001 - unparse failure must not leak the raw line
        return "<snippet unavailable>"
    return text.replace("\n", " ")[:160]


def _mask(text: str, *decoded: str) -> str:
    """Return the source line for the inventory with every URL reduced to scheme://host/path,
    or a marker when the line *or any decoded string literal on it* looks like a credential.
    The decoded values matter: ``"https:\\x2f\\x2fu:pw@h"`` hides ``://`` from a regex over
    the raw line but not from the parsed constant."""
    text = text.strip().replace("\n", " ")
    if _secret_like(text) or any(_secret_like(d) for d in decoded):
        return "<masked: secret-like literal>"
    out = URL_RE.sub(_redact_url, text)
    # Escaped literals ("\\x3f" for "?") hide a query from the in-place redaction. If any piece
    # of a decoded URL's query/fragment/user info survives in the redacted line, replace it all.
    for d in decoded:
        for url in URL_RE.findall(d):
            if any(len(part) >= 3 and part in out for part in _url_data_parts(url)):
                return "<masked: url with data in escaped literal>"
    return out[:160]


def _decoded_strings(node: ast.AST) -> list[str]:
    return [c.value for c in ast.walk(node) if isinstance(c, ast.Constant) and isinstance(c.value, str)]


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


def _is_environ(value: ast.AST) -> bool:
    return (isinstance(value, ast.Attribute) and value.attr == "environ") or (
        isinstance(value, ast.Name) and value.id in {"env", "environ"}
    )


def _endpoint_env_role(name: str) -> str:
    """``AWS_ENDPOINT_URL_<SERVICE>`` follows the service it points at (``AWS_ENDPOINT_URL_SQS`` is
    messaging, not an IAM credential). A service the catalog does not know is ``unknown``."""
    service = service_for_endpoint_env(name)
    return role_for_service(service) if service else "unknown"


def _config_role(name: str) -> str:
    """Role of a configuration variable. ``AWS_SECRET_ACCESS_KEY`` is an IAM credential, not a
    Secrets Manager entry; ``AWS_ENDPOINT_URL_<SERVICE>`` follows the service it points at."""
    if name.startswith(ENDPOINT_ENV_PREFIX):
        return _endpoint_env_role(name)
    if name.startswith("AWS_"):
        return "iam"
    if name.startswith("S3_"):
        return "object_store"
    if name.startswith("KMS_"):
        return "kms"
    return "secrets" if "SECRET" in name else "iam"


def _emulator_lines(text: str) -> set[int]:
    """1-based line numbers of compose services that run a local AWS emulator or its seed CLI."""
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        return set()
    if not isinstance(node, yaml.MappingNode):
        return set()
    lines: set[int] = set()
    for key, services in node.value:
        if key.value != "services" or not isinstance(services, yaml.MappingNode):
            continue
        for _, svc in services.value:
            if not isinstance(svc, yaml.MappingNode):
                continue
            image = next((v.value for k, v in svc.value if k.value == "image" and isinstance(v, yaml.ScalarNode)), "")
            if EMULATOR_IMAGE_RE.search(str(image)):
                end = svc.end_mark.line + (1 if svc.end_mark.column > 0 else 0)
                lines.update(range(svc.start_mark.line + 1, end + 1))
    return lines


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
        self.findings.append(
            Finding(f"dep-{self.counter[0]:04d}", kind, role, service, self.rel, line, node_snippet(node), detail)
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
        if any(kw.arg in PY_FIFO_KWARGS for kw in node.keywords):
            self._feature("sqs_fifo", node)
        # Only environment receivers: ``os.environ.get`` / ``env.get`` / ``os.getenv`` / ``getenv``.
        # A bare ``.get`` (``app.get('/healthz')`` route, ``dict.get``) is not an env reference.
        if (isinstance(func, ast.Attribute) and func.attr == "get" and _is_environ(func.value)) or (
            isinstance(func, ast.Attribute) and func.attr == "getenv") or (
            isinstance(func, ast.Name) and func.id == "getenv"
        ):
            self._env_ref(node, node.args[0] if node.args else None, "os.environ.get")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _is_environ(node.value):
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
            feature = PY_SCOPE_CONSTANTS.get(v.strip()) or ("sqs_fifo" if FIFO_NAME_RE.search(v) else None)
            if feature:
                self._feature(feature, node)

    def _feature(self, feature: str, node: ast.AST) -> None:
        role, service, _ = SCOPE_FEATURES[feature]
        self._new("unsupported_feature", role, service, node, feature=feature)

    def _env_ref(self, node: ast.AST, key: ast.AST | None, how: str) -> None:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            name = key.value
            role = "unknown"
            if name.startswith(ENDPOINT_ENV_PREFIX):
                role = _endpoint_env_role(name)
            elif name.startswith("S3_"):
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


def python_files(root: Path, app_path: str = ".") -> list[Path]:
    return _iter_files((root / app_path).resolve(), {".py"})


def config_files(root: Path) -> list[Path]:
    return [p for p in _iter_files(root, CONFIG_SUFFIXES | {".tf"}) if p.name not in LOCK_FILES]


def scanned_files(root: Path, app_path: str = ".") -> list[Path]:
    """Exactly the files the scanners read: Python under ``app_path``, configuration repo-wide."""
    root = root.resolve()
    return sorted(set(python_files(root, app_path)) | set(config_files(root)))


def scan_python(root: Path, app_path: str = ".") -> list[Finding]:
    counter = [0]
    out: list[Finding] = []
    for path in python_files(root, app_path):
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
    for path in config_files(root):
        rel = _rel(root, path)
        text = path.read_text(encoding="utf-8", errors="replace")
        skip = _emulator_lines(text) if COMPOSE_FILE_RE.match(path.name) else set()
        block, depth = "", 0  # enclosing Terraform resource type and its brace depth
        for lineno, line in enumerate(text.splitlines(), 1):
            if lineno in skip:
                continue
            m = TF_RESOURCE_RE.match(line)
            in_mapping = block == "aws_lambda_event_source_mapping" and depth > 0
            if m:
                block, depth = m.group(1), 0
            depth += line.count("{") - line.count("}")
            features = [f for rx, f in CONFIG_SCOPE_RES if rx.search(line)]
            if in_mapping:
                features += [f for rx, f in TF_MAPPING_SCOPE_RES if rx.search(line)]
            for feature in dict.fromkeys(features):
                counter[0] += 1
                role, service, _ = SCOPE_FEATURES[feature]
                out.append(Finding(f"dep-{counter[0]:04d}", "unsupported_feature", role, service, rel, lineno, _mask(line), {"feature": feature}))
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
                role = _config_role(name)
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
