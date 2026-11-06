"""Model credentials are selected only for the dedicated platform process."""

from pathlib import Path

TELEMETRY_KEYS = {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_CERTIFICATE",
                  "OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE", "OTEL_EXPORTER_OTLP_CLIENT_KEY"}

RUNTIME_KEYS = TELEMETRY_KEYS | {
    "PATH", "LANG", "LC_ALL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ACCESS_TOKEN", "HOME", "TMPDIR",
    "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "PYTHONDONTWRITEBYTECODE",
}


def isolated_environment(original: dict[str, str], runtime: Path, provider="openai") -> dict[str, str]:
    key = {"openai": "OPENAI_API_KEY", "claude": "ANTHROPIC_API_KEY", "chatgpt-plan": "ACCESS_TOKEN"}[provider]
    environment = {name: original[name] for name in ("PATH", "LANG", "LC_ALL", key, *TELEMETRY_KEYS)
                   if name in original}
    environment.update({"HOME": str(runtime), "TMPDIR": str(runtime),
                        "CLAUDE_CONFIG_DIR": str(runtime / "config"),
                        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                        "PYTHONDONTWRITEBYTECODE": "1"})
    return environment
