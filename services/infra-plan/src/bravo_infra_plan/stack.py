"""Load a T45 infra stack: schema, step graph, parameters (ADR-0033).

A stack is data only. It names repository renderers, pinned charts and step kinds; it never
carries a credential value or a shell command. Every check here runs before anything renders.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import jsonschema

PLACEHOLDER = re.compile(r"\$\{([a-z][a-z0-9_]{0,63})\}")
IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"


class StackError(ValueError):
    """A stack, request or render that the planner refuses. The message is a stable code."""


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _schema():
    return json.loads(resources.files("bravo_infra_plan").joinpath("schemas/stack.schema.json").read_text())


@dataclass(frozen=True)
class Stack:
    document: dict
    digest: str
    levels: tuple[tuple[str, ...], ...]

    @property
    def name(self) -> str:
        return self.document["stack"]

    @property
    def steps(self) -> dict:
        return {step["id"]: step for step in self.document["steps"]}

    @property
    def environment(self) -> dict:
        return environment_of(self.document)

    @property
    def kind(self) -> str:
        return self.environment["kind"]

    @property
    def cluster_steps(self) -> bool:
        """True when a step talks to a Kubernetes cluster (anything but terraform)."""
        return any(step["kind"] != "terraform" for step in self.document["steps"])

    @property
    def terraform_steps(self) -> bool:
        return any(step["kind"] == "terraform" for step in self.document["steps"])

    def request_schema(self) -> dict:
        """The JSON Schema every plan request (operator, console or agent `infra.plan`) must pass."""
        params = {}
        for name, spec in self.document["params"].items():
            params[name] = {"type": "string", "minLength": 1, "maxLength": 253}
            if "pattern" in spec:
                params[name]["pattern"] = spec["pattern"]
            if "enum" in spec:
                params[name]["enum"] = spec["enum"]
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["stack", "environment_id", "params"],
            "properties": {
                "stack": {"const": self.name},
                "environment_id": {"type": "string", "pattern": IDENTIFIER},
                "params": {"type": "object", "additionalProperties": False,
                           "required": sorted(params), "properties": params},
            },
        }

    def validate_request(self, request) -> dict:
        try:
            jsonschema.Draft202012Validator(self.request_schema()).validate(request)
        except jsonschema.ValidationError as error:
            raise StackError("invalid_plan_request") from error
        return json.loads(canonical(request))


# A stack without an `environment` block is the first one (target-onprem): these are its names.
ONPREM_DEFAULTS = {
    "kind": "onprem", "app_namespace": "target-dev", "database": "target-db",
    "secret_store": {"kind": "Pod", "namespace": "bravo-secrets", "name": "target-openbao-0"},
}


def environment_of(document) -> dict:
    """The stack's kind and the names its checks read, with `${param}` placeholders left in."""
    declared = document.get("environment")
    if declared is None:
        return json.loads(canonical(ONPREM_DEFAULTS))
    return {k: v for k, v in ONPREM_DEFAULTS.items() if k != "kind"} | declared


def substitute(value, params):
    """`${param}` in strings, recursively through dicts and lists."""
    if isinstance(value, str):
        return PLACEHOLDER.sub(lambda m: params[m.group(1)], value)
    if isinstance(value, dict):
        return {k: substitute(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, params) for v in value]
    return value


def load_stack(path) -> Stack:
    return parse_stack(json.loads(Path(path).read_text()))


def parse_stack(document) -> Stack:
    try:
        jsonschema.Draft202012Validator(_schema()).validate(document)
    except jsonschema.ValidationError as error:
        raise StackError("invalid_stack") from error
    steps = document["steps"]
    ids = [step["id"] for step in steps]
    if len(set(ids)) != len(ids):
        raise StackError("duplicate_step")
    by_id = {step["id"]: step for step in steps}
    paths = [step["path"] for step in steps if step["kind"] == "gitops_publish"]
    if len(set(paths)) != len(paths):
        raise StackError("duplicate_publish_path")
    for step in steps:
        needs = step.get("needs", [])
        if any(need not in by_id or need == step["id"] for need in needs):
            raise StackError("unknown_dependency")
        if step["kind"] == "gitops_sync":
            published = by_id.get(step["publish"])
            if published is None or published["kind"] != "gitops_publish" or step["publish"] not in needs:
                raise StackError("sync_without_publish")
        for value in step.get("vars", {}).values():
            if isinstance(value, dict):
                upstream = by_id.get(value["from"])
                if upstream is None or upstream["kind"] != "terraform" or value["from"] not in needs:
                    raise StackError("terraform_reference_without_need")
        for render in step.get("renders", []):
            if "chart" in render and render["chart"] not in document["charts"]:
                raise StackError("unknown_chart")
            if "chart" in render and ("values" in render) == ("values_file" in render):
                raise StackError("chart_values_required")
        for name in PLACEHOLDER.findall(json.dumps(step)):
            if name not in document["params"]:
                raise StackError("unknown_parameter")
    environment = document.get("environment", {})
    for name in PLACEHOLDER.findall(json.dumps(environment)):
        if name not in document["params"]:
            raise StackError("unknown_parameter")
    endpoint = environment.get("cluster_endpoint")
    if endpoint is not None and by_id.get(endpoint["from"], {}).get("kind") != "terraform":
        raise StackError("cluster_endpoint_without_terraform")
    return Stack(document, sha256_text(canonical(document)), _levels(steps))


def _levels(steps):
    """Kahn's algorithm. Steps on one level have no dependency between them and run in parallel."""
    remaining = {step["id"]: set(step.get("needs", [])) for step in steps}
    order = [step["id"] for step in steps]
    levels, done = [], set()
    while remaining:
        ready = tuple(i for i in order if i in remaining and remaining[i] <= done)
        if not ready:
            raise StackError("stack_cycle")
        levels.append(ready)
        done.update(ready)
        for i in ready:
            del remaining[i]
    return tuple(levels)
