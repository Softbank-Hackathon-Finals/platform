"""Model credentials are selected only for the dedicated platform process."""

import re
from pathlib import Path

TELEMETRY_KEYS = {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_CERTIFICATE",
                  "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", "OTEL_EXPORTER_OTLP_CLIENT_KEY"}

RUNTIME_KEYS = TELEMETRY_KEYS | {
    "PATH", "LANG", "LC_ALL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ACCESS_TOKEN", "HOME", "TMPDIR",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "PYTHONDONTWRITEBYTECODE",
    # The session VM's Docker endpoint for the Agents SDK sandbox (configuration, not a credential
    # for the model: the model only sees tool results from inside the no-network container).
    "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
}
DOCKER_KEYS = ("DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")


def isolated_environment(original: dict[str, str], runtime: Path, provider="openai") -> dict[str, str]:
    key = {"openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY", "chatgpt-plan": "ACCESS_TOKEN"}[provider]
    environment = {name: original[name] for name in ("PATH", "LANG", "LC_ALL", key, *TELEMETRY_KEYS, *DOCKER_KEYS)
                   if name in original}
    environment.update({"HOME": str(runtime), "TMPDIR": str(runtime),
                        "CLAUDE_CONFIG_DIR": str(runtime / "config"),
                        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                        "PYTHONDONTWRITEBYTECODE": "1"})
    return environment



def effort(value):
    """Reasoning effort from the environment's model config, or None for the provider default.

    Only the shape is checked here; which levels a model accepts is the provider's contract,
    and the chosen value lives in configuration, not code.
    """
    if value is not None and (not isinstance(value, str) or not re.fullmatch(r"[a-z]{1,16}", value)):
        raise ValueError("invalid reasoning effort")
    return value
