"""Allowlisted SDK execution metadata exported to the platform OTEL collector.

No SDK-default cloud exporter, prompt, response text, arguments, exception text or credentials
are exported. The agent worker is a dedicated process; its SDK processor is installed once.
"""

import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from urllib.parse import urlparse

from agents.tracing import TracingProcessor, set_trace_processors
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_active = ContextVar("bravo_agent_observation", default=None)
_lock = threading.Lock()
_installed = False
_provider = None
_export_problem = None
SAFE_TOOLS = {"repo_read", "plan_propose", "policy_evaluate", "inventory_describe", "policy_denied"}


def tracer_provider():
    global _provider, _export_problem
    with _lock:
        if _provider is None:
            _export_problem = None
            provider = TracerProvider(resource=Resource.create({"service.name": "bravo-agent-worker"}))
            endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT")
            if endpoint:
                try:
                    parsed = urlparse(endpoint)
                    internal = parsed.hostname and parsed.hostname.endswith((".svc", ".svc.cluster.local"))
                    if (parsed.username or parsed.password or parsed.query or parsed.fragment
                            or not parsed.hostname or parsed.path != "/v1/traces"
                            or not (parsed.scheme == "https" or parsed.scheme == "http" and internal)):
                        raise ValueError("invalid endpoint")
                    if parsed.port is not None and not 1 <= parsed.port <= 65535:
                        raise ValueError("invalid port")
                    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
                        endpoint=endpoint, timeout=3,
                        certificate_file=os.environ.get("OTEL_EXPORTER_OTLP_CERTIFICATE"),
                        client_certificate_file=os.environ.get("OTEL_EXPORTER_OTLP_CLIENT_CERTIFICATE"),
                        client_key_file=os.environ.get("OTEL_EXPORTER_OTLP_CLIENT_KEY"),
                    ), max_queue_size=256, max_export_batch_size=64, schedule_delay_millis=1000))
                except Exception:
                    # Telemetry misconfiguration must not consume a terminal model attempt.
                    # Cache the local-only provider and record a code-owned diagnostic below.
                    _export_problem = "INVALID_EXPORTER_CONFIGURATION"
            _provider = provider
        return _provider


class SDKToOTEL(TracingProcessor):
    """Consume lifecycle callbacks; never serialize the SDK's span export payload."""

    def on_trace_start(self, trace):
        pass

    def on_trace_end(self, trace):
        pass

    def on_span_start(self, span):
        current = _active.get()
        if current is None or span.trace_id != current.sdk_trace_id:
            return
        kind = span.span_data.type
        if kind not in {"agent", "task", "turn", "generation", "function", "response"}:
            return
        parent = current.spans.get(span.parent_id, current.root)
        attributes = {"gen_ai.provider.name": "openai", "bravo.sdk.span_type": kind}
        if kind == "function":
            name = span.span_data.name.removeprefix("bravo.")
            attributes["gen_ai.tool.name"] = name if name in SAFE_TOOLS else "unlisted"
        current.spans[span.span_id] = current.tracer.start_span(
            "agents." + kind, context=trace.set_span_in_context(parent), attributes=attributes)

    def on_span_end(self, span):
        current = _active.get()
        target = current.spans.pop(span.span_id, None) if current else None
        if target is None:
            return
        usage = getattr(span.span_data, "usage", None)
        if isinstance(usage, dict):
            for key in ("input_tokens", "output_tokens", "total_tokens"):
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    target.set_attribute("gen_ai.usage." + key, value)
        if span.error:
            target.set_status(trace.StatusCode.ERROR)
        target.end()

    def shutdown(self):
        pass

    def force_flush(self):
        if _provider is not None:
            _provider.force_flush(timeout_millis=3000)


def install_processor():
    global _installed
    with _lock:
        if not _installed:
            # Replace the SDK default exporter: SIWC tokens are not tracing API credentials.
            set_trace_processors([SDKToOTEL()])
            _installed = True


class Observation:
    def __init__(self, tracer, root):
        self.tracer, self.root, self.spans = tracer, root, {}
        self.trace_id = format(root.get_span_context().trace_id, "032x")
        self.sdk_trace_id = "trace_" + self.trace_id


@contextmanager
def agent_observation(tools, sdk, model):
    install_processor()
    provider = tracer_provider()
    if _export_problem:
        tools.record("provider", "OBSERVED", "TELEMETRY_EXPORT_DISABLED", {"category": _export_problem})
    tracer = provider.get_tracer("bravo.agent")
    try:
        with tracer.start_as_current_span("agent.run", record_exception=False,
                                         set_status_on_exception=False, attributes={
            "bravo.run_id": tools.bundle.run_id, "bravo.attempt": tools.attempt,
            "bravo.source_sha": tools.bundle.source_sha, "bravo.agent.sdk": sdk,
            "gen_ai.request.model": model,
        }) as root:
            observation = Observation(tracer, root)
            tools.observability = {"trace_id": observation.trace_id, "sdk": sdk,
                                   "sdk_trace_id": observation.sdk_trace_id if sdk == "openai_agents" else None,
                                   "export_configuration": _export_problem or "VALID"}
            token = _active.set(observation)
            try:
                yield observation
            except BaseException:
                root.set_status(trace.StatusCode.ERROR)
                raise
            finally:
                for span in observation.spans.values():
                    span.end()
                _active.reset(token)
                for key in ("requests", "total_tokens"):
                    value = tools.usage.get(key)
                    if type(value) is int and value >= 0:
                        root.set_attribute("gen_ai.usage." + key, value)
    finally:
        provider.force_flush(timeout_millis=3000)


@contextmanager
def tool_observation(name):
    current = _active.get()
    if current is None:
        yield
        return
    short_name = name.removeprefix("mcp__bravo__")
    with current.tracer.start_as_current_span(
        "claude.function", record_exception=False, set_status_on_exception=False,
        attributes={"gen_ai.tool.name": short_name if short_name in SAFE_TOOLS else "unlisted"},
    ) as span:
        try:
            yield
        except BaseException:
            span.set_status(trace.StatusCode.ERROR)
            raise
