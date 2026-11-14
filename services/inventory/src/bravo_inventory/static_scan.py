"""Static detection of cloud dependencies in application code and configuration.

Python is parsed with ``ast`` (no regex guessing on call sites). Configuration files
(compose, Kubernetes YAML, Terraform, .env examples) are scanned for environment
variable names, cloud endpoints, ARNs and hard-coded regions. Every finding carries the
file, line and a masked snippet so a reviewer can verify it without the model.

What a piece of evidence means depends on the source kind (ADR-0034 ``source.static_rules``):
the rules come from ``source_rules.STATIC_RULES`` and every finding records the kind it was
read under and whether it ties the app to a public cloud (``cloud_dependency``).
"""

from __future__ import annotations

import ast
import copy
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from urllib.parse import unquote, urlsplit

import yaml

from .catalog import OTHER_CLOUD_PREFIXES, role_for_service
from .source_rules import (
    S3_ENDPOINT_ENV,
    TF_RESOURCE_RE,
    StaticRules,
    Unsupported,
    UnsupportedKind,
    gcp_service_role,
    gcp_tf_role,
    static_rules_for,
)

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
ENV_LINE_RE = re.compile(r"^\s*(?:-\s*)?([A-Z][A-Z0-9_]{2,})\s*[:=]\s*(.*)$")

# `.github`: CI pipeline configuration is not the app's runtime configuration.
SKIP_DIRS = {".git", ".github", ".venv", "node_modules", "__pycache__", "dist", "build", ".terraform", "tests", "test"}
CONFIG_SUFFIXES = {".yaml", ".yml", ".env", ".example", ".toml", ".ini", ".cfg", ".json"}
LOCK_FILES = {"uv.lock", "pnpm-lock.yaml"}
COMPOSE_FILE_RE = re.compile(r"^(?:docker-)?compose(?:\.[\w-]+)?\.ya?ml$")
# Local AWS emulators and the CLI containers that seed them: their env configures the emulator,
# not the app (the app's own compose env stays in scope).
EMULATOR_IMAGE_RE = re.compile(r"(?:^|/)(?:moto|motoserver|localstack|aws-cli)(?:[:@]|$)")
ENDPOINT_LABELS = {"aws": "amazonaws.com", "gcp": "googleapis.com"}
BOTO_MODULES = frozenset({"boto3", "botocore", "aiobotocore", "aioboto3"})

# Source kind used when a caller does not pass one (CLI ``--source-kind`` sets it for the run).
_SOURCE_KIND: ContextVar[str] = ContextVar("bravo_inventory_source_kind", default="aws")


@contextmanager
def use_source_kind(kind: str) -> Iterator[None]:
    """Scan with ``kind``'s rules inside the block when the caller does not pass a kind
    (``report.build_inventory`` calls ``scan_repo`` without one)."""
    token = _SOURCE_KIND.set(kind)
    try:
        yield
    finally:
        _SOURCE_KIND.reset(token)


def current_source_kind() -> str:
    return _SOURCE_KIND.get()


def rules_or_raise(kind: str | None) -> StaticRules:
    rules = static_rules_for(kind or current_source_kind())
    if isinstance(rules, Unsupported):
        raise UnsupportedKind(rules)
    return rules

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
    # Source kind whose rules produced the finding, and whether the evidence ties the app to a
    # public cloud (on-prem ``boto3`` + custom ``endpoint_url`` is S3-compatible: ``False``).
    source_kind: str = "aws"
    cloud_dependency: bool = True

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


def _dotted(node: ast.AST) -> str:
    """``google.cloud.storage.Client`` for an attribute chain rooted at a name, else ``""``."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _gcp_module_service(module: str) -> str | None:
    """``google.cloud.storage`` → ``storage``; ``google.auth[...]`` → ``auth``; else ``None``."""
    parts = module.split(".")
    if parts[:2] == ["google", "cloud"] and len(parts) >= 3:
        return parts[2]
    if parts[:2] in (["google", "auth"], ["google", "oauth2"]):
        return parts[1]
    return None


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
    def __init__(self, rel: str, lines: list[str], counter: list[int], rules: StaticRules):
        self.rel, self.lines, self.counter, self.rules = rel, lines, counter, rules
        self.findings: list[Finding] = []
        self.boto_aliases: set[str] = set()
        # local name → google-cloud service (``storage``, ``secretmanager``, ``auth`` ...)
        self.gcp_aliases: dict[str, str] = {}
        self.google_imported = False  # ``import google.cloud.storage`` binds ``google``
        self.hvac_aliases: set[str] = set()

    def _new(self, kind: str, role: str, service: str, node: ast.AST, *, cloud_dep: bool = True, **detail: str) -> None:
        self.counter[0] += 1
        line = getattr(node, "lineno", 0)
        self.findings.append(
            Finding(f"dep-{self.counter[0]:04d}", kind, role, service, self.rel, line, node_snippet(node), detail,
                    source_kind=self.rules.kind, cloud_dependency=cloud_dep)
        )

    def _other_cloud(self, name: str, node: ast.AST) -> None:
        for prefix, cloud in OTHER_CLOUD_PREFIXES:
            if cloud == self.rules.kind:
                continue  # the source's own SDK is classified by role, not flagged as "other cloud"
            if name.startswith(prefix):
                self._new("other_cloud_import", "unknown", name, node, cloud=cloud)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.name
            if name.split(".")[0] in BOTO_MODULES:
                self.boto_aliases.add(alias.asname or name.split(".")[0])
                # On an S3-compatible source the import alone is not AWS: the client call decides.
                self._new("sdk_import", "unknown", name.split(".")[0], node, cloud_dep=not self.rules.s3_compatible)
            if self.rules.gcp_sdk and (service := _gcp_module_service(name)) is not None:
                if alias.asname:
                    self.gcp_aliases[alias.asname] = service
                else:
                    self.google_imported = True
                self._new("sdk_import", "unknown", name, node)
            if self.rules.vault_sdk and name.split(".")[0] == "hvac":
                self.hvac_aliases.add(alias.asname or "hvac")
                self._new("sdk_import", "unknown", "hvac", node, cloud_dep=False)
            self._other_cloud(name, node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        if mod.split(".")[0] in {"boto3", "botocore"}:
            self._new("sdk_import", "unknown", mod, node, cloud_dep=not self.rules.s3_compatible)
        if self.rules.gcp_sdk and (mod in {"google", "google.cloud"} or _gcp_module_service(mod)):
            for alias in node.names:
                if mod == "google":
                    service = _gcp_module_service(f"google.{alias.name}")
                elif mod == "google.cloud":
                    service = alias.name
                else:
                    service = _gcp_module_service(mod)
                if service:
                    self.gcp_aliases[alias.asname or alias.name] = service
            self._new("sdk_import", "unknown", mod, node)
        if self.rules.vault_sdk and mod.split(".")[0] == "hvac":
            self.hvac_aliases.update(a.asname or a.name for a in node.names)
            self._new("sdk_import", "unknown", "hvac", node, cloud_dep=False)
        self._other_cloud(mod, node)
        self.generic_visit(node)

    def _gcp_call(self, node: ast.Call) -> None:
        dotted = _dotted(node.func)
        if not dotted:
            return
        parts = dotted.split(".")
        service: str | None = None
        if parts[0] == "google" and self.google_imported:
            service = _gcp_module_service(".".join(parts[:3]))
        elif parts[0] in self.gcp_aliases:
            service = self.gcp_aliases[parts[0]]
        if not service:
            return
        # Client factories (``storage.Client``, ``SecretManagerServiceClient``); for auth modules
        # every call obtains or loads credentials (``google.auth.default()``).
        if service not in {"auth", "oauth2"} and not any(p.endswith("Client") for p in parts):
            return
        detail = {"factory": dotted}
        if any(kw.arg == "client_options" for kw in node.keywords):
            detail["endpoint"] = "configurable"
        self._new("sdk_call", gcp_service_role(service), service, node, **detail)

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
                cloud = True
                if self.rules.s3_compatible and service == "s3" and "endpoint_url" in detail:
                    # A custom endpoint makes this an S3-compatible store (SeaweedFS), unless the
                    # literal endpoint is itself a cloud endpoint.
                    cloud = self.rules.endpoint_cloud(detail["endpoint_url"]) is not None
                    if not cloud:
                        detail["compat"] = "s3-compatible"
                self._new("sdk_call", role_for_service(service) if service else "unknown", service or "unknown", node, cloud_dep=cloud, **detail)
        if self.rules.gcp_sdk:
            self._gcp_call(node)
        if self.hvac_aliases:
            dotted = _dotted(func)
            if dotted and dotted.split(".")[0] in self.hvac_aliases and dotted.endswith("Client"):
                self._new("sdk_call", "secrets", "hvac", node, cloud_dep=False, factory=dotted)
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
            region = self.rules.region
            if (cloud := self.rules.endpoint_cloud(v)) is not None:
                self._new("endpoint_literal", "unknown", cloud, node, value=safe_endpoint(v))
            elif ARN_RE.search(v):
                self._new("arn_literal", "unknown", "aws", node, value=v[:120])
            elif region is not None and region[0].fullmatch(v):
                self._new("region_literal", "unknown", region[1], node, value=v)

    def _env_ref(self, node: ast.AST, key: ast.AST | None, how: str) -> None:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            name = key.value
            env = self.rules.code_env(name)
            self._new("env_ref", env.role, "env", node, cloud_dep=env.cloud_dependency, name=name, how=how)


def python_files(root: Path, app_path: str = ".") -> list[Path]:
    return _iter_files((root / app_path).resolve(), {".py"})


def config_files(root: Path) -> list[Path]:
    return [p for p in _iter_files(root, CONFIG_SUFFIXES | {".tf"}) if p.name not in LOCK_FILES]


def scanned_files(root: Path, app_path: str = ".") -> list[Path]:
    """Exactly the files the scanners read: Python under ``app_path``, configuration repo-wide."""
    root = root.resolve()
    return sorted(set(python_files(root, app_path)) | set(config_files(root)))


def scan_python(root: Path, app_path: str = ".", source_kind: str | None = None) -> list[Finding]:
    rules = rules_or_raise(source_kind)
    counter = [0]
    out: list[Finding] = []
    for path in python_files(root, app_path):
        text = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            counter[0] += 1
            out.append(Finding(f"dep-{counter[0]:04d}", "parse_error", "unknown", "python", _rel(root, path), 0, "", {},
                               source_kind=rules.kind, cloud_dependency=False))
            continue
        visitor = _PyVisitor(_rel(root, path), text.splitlines(), counter, rules)
        visitor.visit(tree)
        out.extend(visitor.findings)
    return out


def _tf_role(rtype: str, cloud: str) -> str:
    if cloud == "gcp":
        return gcp_tf_role(rtype)
    return role_for_service(rtype.removeprefix("aws_").split("_")[0])


def scan_config(root: Path, start_index: int, source_kind: str | None = None) -> list[Finding]:
    """Compose / Kubernetes YAML / env examples / Terraform. Values are masked, names kept."""
    rules = rules_or_raise(source_kind)
    counter = [start_index]
    out: list[Finding] = []

    def add(kind: str, role: str, service: str, rel: str, lineno: int, snippet: str, detail: dict, cloud: bool) -> None:
        counter[0] += 1
        out.append(Finding(f"dep-{counter[0]:04d}", kind, role, service, rel, lineno, snippet, detail,
                           source_kind=rules.kind, cloud_dependency=cloud))

    for path in config_files(root):
        rel = _rel(root, path)
        text = path.read_text(encoding="utf-8", errors="replace")
        skip = _emulator_lines(text) if COMPOSE_FILE_RE.match(path.name) else set()
        for lineno, line in enumerate(text.splitlines(), 1):
            if lineno in skip:
                continue
            m = TF_RESOURCE_RE.match(line)
            provider = next((c for prefix, c in rules.tf_providers if m.group(1).startswith(prefix)), None) if m else None
            if m and provider:
                detail = {"name": m.group(2)}
                if provider != rules.kind:
                    detail["cloud"] = provider
                add("iac_resource", _tf_role(m.group(1), provider), m.group(1), rel, lineno, _mask(line), detail, True)
                continue
            m = ENV_LINE_RE.match(line)
            env = rules.config_env(m.group(1)) if m else None
            if m and env is not None:
                name, value = m.group(1), m.group(2)
                detail = {"name": name}
                secret_name = SECRET_NAME_RE.search(name) is not None
                cloud = rules.endpoint_cloud(value)
                if cloud is not None:
                    detail["endpoint"] = ENDPOINT_LABELS[cloud]
                elif "://" in value:
                    detail["endpoint"] = safe_endpoint(value)
                elif (safe := safe_value(name, value)) is not None:
                    detail["value"] = safe
                masked = secret_name or (bool(value.strip()) and "value" not in detail and "endpoint" not in detail)
                snippet = f"{name}: <masked>" if masked else _mask(line)
                add("config_env", env.role, "env", rel, lineno, snippet, detail, env.cloud_dependency or cloud is not None)
                continue
            if ARN_RE.search(line):
                add("arn_literal", "unknown", "aws", rel, lineno, _mask(line), {}, True)
    return out


def _s3_endpoint_from_env(findings: list[Finding], rules: StaticRules) -> list[Finding]:
    """On an S3-compatible source, ``AWS_ENDPOINT_URL[_S3]`` in the app's configuration points
    every S3 client without ``endpoint_url`` at the compatible store (botocore reads it)."""
    if not rules.s3_compatible:
        return findings
    names = sorted({f.detail.get("name", "") for f in findings if f.kind in {"env_ref", "config_env"}} & S3_ENDPOINT_ENV)
    if not names:
        return findings
    out = []
    for f in findings:
        if f.kind == "sdk_call" and f.service == "s3" and "endpoint_url" not in f.detail:
            f = replace(f, cloud_dependency=False, detail={
                **f.detail, "endpoint_url": "configurable", "endpoint_from": f"env:{names[-1]}", "compat": "s3-compatible",
            })
        out.append(f)
    return out


def scan_repo(root: Path, app_path: str = ".", source_kind: str | None = None) -> list[Finding]:
    """Scan with the rules of ``source_kind`` (default: the kind set by ``use_source_kind``,
    else ``aws``). An unregistered kind raises ``UnsupportedKind``; it is never scanned with
    another kind's rules."""
    rules = rules_or_raise(source_kind)
    root = root.resolve()
    py = scan_python(root, app_path, rules.kind)
    return _s3_endpoint_from_env(py + scan_config(root, len(py), rules.kind), rules)
