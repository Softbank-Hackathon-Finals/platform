import asyncio
import json

import pytest
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from test_openai import install_sdk

from bravo_agent import telemetry
from bravo_agent.coordinator import Coordinator
from bravo_agent.openai_provider import OpenAIProvider
from bravo_agent.runtime import isolated_environment


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "bravo-agent-worker"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "_provider", provider)
    monkeypatch.setattr(telemetry, "_export_problem", None)
    yield exporter
    provider.shutdown()


def test_native_runner_spans_share_trace_with_result_and_omit_payloads(context, monkeypatch, spans):
    bundle, snapshot, proposal, _ = context
    requests = install_sdk(monkeypatch, [
        {"name": "repo_read", "arguments": json.dumps({"path": "src/config.py"})},
        {"name": "rehearsal_exec", "arguments": json.dumps({"command": "aws s3 rb s3://private-canary --force"})},
        {"name": "plan_propose", "arguments": json.dumps(proposal)},
    ])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model")))
    assert result["state"] == "PROPOSAL_READY" and len(requests) == 3
    observed = result["attempts"][0]["observability"]
    completed = spans.get_finished_spans()
    assert {span.context.trace_id for span in completed} == {int(observed["trace_id"], 16)}
    assert observed["sdk_trace_id"] == "trace_" + observed["trace_id"]
    # These come from the real SDK runtime, not handcrafted provider-loop spans.
    assert {"agents.task", "agents.turn", "agents.agent", "agents.generation", "agents.function", "agent.run"} <= {span.name for span in completed}
    assert {span.attributes.get("gen_ai.tool.name") for span in completed} >= {"repo_read", "plan_propose", "policy_denied"}
    assert all(span.parent is not None for span in completed if span.name != "agent.run")
    root, = [span for span in completed if span.name == "agent.run"]
    assert root.attributes["gen_ai.usage.total_tokens"] == 450
    serialized = json.dumps([json.loads(span.to_json()) for span in completed])
    for private in ("private-canary", "model-fixture", "DB_HOST", "src/config.py", "replacement", "rationale"):
        assert private not in serialized
    assert '"input"' not in serialized and '"output"' not in serialized


def test_reused_call_id_for_different_action_stops_before_proposal(context, monkeypatch, spans):
    bundle, snapshot, proposal, _ = context
    install_sdk(monkeypatch, [
        {"name": "repo_read", "arguments": json.dumps({"path": "src/config.py"}), "call_id": "same"},
        {"name": "plan_propose", "arguments": json.dumps(proposal), "call_id": "same"},
    ])
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model")))
    assert result["state"] == "NEEDS_HUMAN" and result["proposal"] is None
    assert not any(event["tool"] == "plan.propose" for event in result["audit"])
    root, = [span for span in spans.get_finished_spans() if span.name == "agent.run"]
    assert root.status.is_ok is False
    assert not root.events  # No exception text/stack is exported.


def test_telemetry_environment_does_not_inherit_exporter_credentials(tmp_path):
    result = isolated_environment({
        "OPENAI_API_KEY": "model-fixture",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://otel.bravo-observability.svc.cluster.local:4318/v1/traces",
        "OTEL_EXPORTER_OTLP_HEADERS": "Authorization=must-not-export",
        "OTEL_RESOURCE_ATTRIBUTES": "secret=must-not-export",
        "OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA": "true",
    }, tmp_path)
    assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" in result
    assert "must-not-export" not in json.dumps(result)
    assert "OPENAI_AGENTS_TRACE_INCLUDE_SENSITIVE_DATA" not in result


@pytest.mark.parametrize("endpoint", ["http://public.example/v1/traces", "https://key@host/v1/traces",
                                      "https://host/v1/traces?token=secret", "https://host/wrong"])
def test_invalid_telemetry_destinations_disable_export_without_stopping_model(context, monkeypatch, endpoint):
    bundle, snapshot, proposal, _ = context
    requests = install_sdk(monkeypatch, [{"name": "plan_propose", "arguments": json.dumps(proposal)}])
    monkeypatch.setattr(telemetry, "_provider", None)
    monkeypatch.setattr(telemetry, "_export_problem", None)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    exported = []
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", lambda **kwargs: exported.append(kwargs))
    for _ in range(2):
        result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model")))
        assert result["state"] == "PROPOSAL_READY"
        assert result["attempts"][0]["observability"]["export_configuration"] == "INVALID_EXPORTER_CONFIGURATION"
        assert result["audit"][0]["reason"] == "TELEMETRY_EXPORT_DISABLED"
        assert endpoint not in json.dumps(result)
    assert len(requests) == 2 and not exported
    telemetry._provider.shutdown()


@pytest.mark.parametrize("hostname", ["bravo-otel.bravo-observability.svc", "bravo-otel.bravo-observability.svc.cluster.local"])
def test_both_cluster_dns_forms_are_supported(monkeypatch, hostname):
    monkeypatch.setattr(telemetry, "_provider", None)
    monkeypatch.setattr(telemetry, "_export_problem", None)
    endpoint = f"http://{hostname}:4318/v1/traces"
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", endpoint)
    captured = []
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", lambda **kwargs: captured.append(kwargs))
    # Do not run a batch thread or network exporter in this configuration test.
    from opentelemetry.sdk.trace import SpanProcessor
    monkeypatch.setattr(telemetry, "BatchSpanProcessor", lambda *_args, **_kwargs: SpanProcessor())
    telemetry.tracer_provider()
    assert captured[0]["endpoint"] == endpoint and telemetry._export_problem is None
    telemetry._provider.shutdown()


@pytest.mark.parametrize("subscription", [False, True])
@pytest.mark.parametrize("output_kind,expected", [("message", "PROPOSAL_READY"),
                                                ("custom_tool_call", "NEEDS_HUMAN")])
def test_message_with_function_remains_allowed_but_executable_siblings_are_denied(
        context, monkeypatch, spans, subscription, output_kind, expected):
    from openai.types.responses import ResponseCustomToolCall, ResponseOutputMessage
    from test_subscription_provider import completed, install_stream

    bundle, snapshot, proposal, _ = context
    if subscription:
        requests = install_stream(monkeypatch, [
            [completed("repo_read", {"path": "src/config.py"})], [completed("plan_propose", proposal)],
        ])
    else:
        requests = install_sdk(monkeypatch, [
            {"name": "repo_read", "arguments": json.dumps({"path": "src/config.py"})},
            {"name": "plan_propose", "arguments": json.dumps(proposal)},
        ])
    response = OpenAIProvider.response

    async def with_sibling(self, *args, **kwargs):
        result = await response(self, *args, **kwargs)
        if len(requests) == 1:
            if output_kind == "message":
                sibling = ResponseOutputMessage(id="msg_fixture", type="message", role="assistant",
                    status="completed", content=[{"type": "output_text", "text": "private-message-canary", "annotations": []}])
            else:
                sibling = ResponseCustomToolCall(id="custom_fixture", type="custom_tool_call",
                    name="shell", call_id="call_unoffered", input="private-command-canary")
            result = result.model_copy(update={"output": [sibling, *result.output]})
        return result

    monkeypatch.setattr(OpenAIProvider, "response", with_sibling)
    result = asyncio.run(Coordinator(bundle, snapshot).propose(OpenAIProvider("fixture-model", subscription=subscription)))
    assert result["state"] == expected
    assert len(requests) == (2 if output_kind == "message" else 1)
    if output_kind != "message":
        assert not any(e["tool"] == "repo.read" for e in result["audit"])
    serialized = json.dumps(result) + json.dumps([json.loads(s.to_json()) for s in spans.get_finished_spans()])
    assert "private-message-canary" not in serialized and "private-command-canary" not in serialized
