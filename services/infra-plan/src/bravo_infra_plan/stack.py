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

    def request_schema(self) -> dict:
        """The JSON Schema every plan request (operator, console or agent `infra.plan`) must pass."""
        params = {}
        for name, spec in self.document["params"].items():
            params[name] = {"type": "string", "minLength": 1, "maxLength": 253}
            if "pattern" in spec:
                params[name]["pattern"] = spec["pattern"]
            if "enum" in spec:
                params[name]["enum"] = spec["enum"]
        required = sorted(n for n, spec in self.document["params"].items() if "default" not in spec)
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["stack", "environment_id", "params"],
            "properties": {
                "stack": {"const": self.name},
                "environment_id": {"type": "string", "pattern": IDENTIFIER},
                "params": {"type": "object", "additionalProperties": False,
                           "required": required, "properties": params},
            },
        }

    def validate_request(self, request) -> dict:
        try:
            jsonschema.Draft202012Validator(self.request_schema()).validate(request)
        except jsonschema.ValidationError as error:
            raise StackError("invalid_plan_request") from error
        request = json.loads(canonical(request))
        # Defaults are written into the request, so the plan (and its digest) names every value it used.
        for name, spec in self.document["params"].items():
            if "default" in spec:
                request["params"].setdefault(name, spec["default"])
        return request

    def applies(self, step_id, params) -> bool:
        when = self.steps[step_id].get("when")
        return when is None or params[when["param"]] in when["in"]


def load_stack(path) -> Stack:
    return parse_stack(json.loads(Path(path).read_text()))


def parse_stack(document) -> Stack:
    try:
        jsonschema.Draft202012Validator(_schema()).validate(document)
    except jsonschema.ValidationError as error:
        raise StackError("invalid_stack") from error
    for spec in document["params"].values():
        if "default" in spec and "enum" in spec and spec["default"] not in spec["enum"]:
            raise StackError("invalid_parameter_default")
    steps = document["steps"]
    for step in steps:
        when = step.get("when")
        if when is not None:
            spec = document["params"].get(when["param"])
            # Only a closed set of values can decide which steps exist; a free-form value cannot.
            if spec is None or "enum" not in spec or not set(when["in"]) <= set(spec["enum"]):
                raise StackError("invalid_step_condition")
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
