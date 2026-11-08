import pytest

from bravo_function_runtime.config import Binding, ConfigError, check_queue, parse

BASE = {"function_name": "pii-check", "handler": "checks.handler", "queue_name": "message-created"}


def test_minimal_binding_defaults_match_lambda():
    b = parse({"bindings": [BASE]}).bindings[0]
    assert (b.module, b.function, b.batch_size, b.timeout_s, b.max_concurrency) == ("checks", "handler", 10, 3.0, 1)


def test_unsupported_settings_are_all_reported_before_start():
    raw = {"bindings": [BASE | {"queue_name": "q.fifo", "filter_criteria": {}, "function_response_types": ["ReportBatchItemFailures"],
                                "maximum_batching_window_s": 5, "mystery": 1}]}
    with pytest.raises(ConfigError) as e:
        parse(raw)
    text = " | ".join(e.value.problems)
    for word in ("FIFO", "필터", "배치 일부 실패", "배치 대기", "mystery"):
        assert word in text


@pytest.mark.parametrize("field,value", [("batch_size", 11), ("timeout_s", 901), ("max_concurrency", 0),
                                         ("handler", "nodot"), ("pass_env", ["lower"])])
def test_out_of_range_values_are_rejected(field, value):
    with pytest.raises(ConfigError):
        parse({"bindings": [BASE | {field: value}]})


def test_duplicate_function_names_are_rejected():
    with pytest.raises(ConfigError):
        parse({"bindings": [BASE, BASE]})


def test_queue_visibility_must_cover_function_timeout():
    b = Binding(function_name="f", handler="m.h", queue_name="q", timeout_s=30)
    assert check_queue(b, {"VisibilityTimeout": "30"}) == []
    assert "짧다" in check_queue(b, {"VisibilityTimeout": "10"})[0]
    assert "FIFO" in check_queue(b, {"VisibilityTimeout": "30", "FifoQueue": "true"})[0]


@pytest.mark.parametrize("name", ["ADMIN_TOKEN", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL_SQS", "WRITE_BLOCKED"])
def test_runtime_secrets_cannot_be_passed_to_the_handler(name):
    with pytest.raises(ConfigError) as e:
        parse({"bindings": [BASE | {"pass_env": ["DB_DSN", name]}]})
    assert name in e.value.problems[0]
