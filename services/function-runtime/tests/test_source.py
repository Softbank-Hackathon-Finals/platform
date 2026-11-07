import pytest

from bravo_function_runtime.config import Binding
from bravo_function_runtime.source import check_source

B = Binding(function_name="pii-check", handler="pii_check.handler", queue_name="message-created",
            batch_size=10, timeout_s=5, max_concurrency=2)
# AWS get-event-source-mapping / get-function-configuration 출력 형식. 계정 ID는 예시 값이다.
MAPPING = {"UUID": "u", "BatchSize": 10, "MaximumBatchingWindowInSeconds": 0,
           "EventSourceArn": "arn:aws:sqs:ap-northeast-1:000000000000:message-created",
           "FunctionArn": "arn:aws:lambda:ap-northeast-1:000000000000:function:pii-check", "State": "Enabled",
           "ScalingConfig": {"MaximumConcurrency": 2}}
FUNCTION = {"FunctionName": "pii-check", "Runtime": "python3.12", "Handler": "pii_check.handler", "Timeout": 5,
            "MemorySize": 128, "PackageType": "Zip"}


def test_matching_source_settings_pass():
    assert check_source(B, MAPPING, FUNCTION) == []


@pytest.mark.parametrize("mapping,function,word", [
    ({"FilterCriteria": {"Filters": [{"Pattern": "{}"}]}}, {}, "이벤트 필터"),
    ({"FunctionResponseTypes": ["ReportBatchItemFailures"]}, {}, "배치 일부 실패"),
    ({"MaximumBatchingWindowInSeconds": 5}, {}, "배치 대기"),
    ({"BatchSize": 5}, {}, "배치 크기"),
    ({"ScalingConfig": {"MaximumConcurrency": 10}}, {}, "동시 실행"),
    ({"EventSourceArn": "arn:aws:sqs:ap-northeast-1:000000000000:other"}, {}, "다르다"),
    ({}, {"Runtime": "nodejs20.x"}, "Python"),
    ({}, {"PackageType": "Image"}, "Python"),
    ({}, {"Handler": "other.handler"}, "핸들러"),
    ({}, {"Timeout": 30}, "제한 시간"),
    ({}, {"Layers": [{"Arn": "x"}]}, "레이어"),
])
def test_source_features_the_runtime_cannot_reproduce_are_rejected(mapping, function, word):
    problems = check_source(B, MAPPING | mapping, FUNCTION | function)
    assert any(word in p for p in problems), problems
