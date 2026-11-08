from fakes import FakeQueue

from bravo_function_runtime import event
from bravo_function_runtime.config import Binding


def test_records_have_the_keys_lambda_gives_to_an_sqs_handler():
    q = FakeQueue(bodies=['{"idempotency_key": "k1"}'])
    evt = event.event(q.receive(10, 0), q.arn(), "ap-northeast-1")
    r = evt["Records"][0]
    assert set(r) == {"messageId", "receiptHandle", "body", "attributes", "messageAttributes", "md5OfBody",
                      "eventSource", "eventSourceARN", "awsRegion"}
    assert r["eventSource"] == "aws:sqs" and r["eventSourceARN"].endswith(":q")
    assert set(r["attributes"]) == {"ApproximateReceiveCount", "SentTimestamp", "SenderId", "ApproximateFirstReceiveTimestamp"}
    assert r["messageAttributes"]["kind"] == {"stringValue": "message.created", "stringListValues": [],
                                              "binaryListValues": [], "dataType": "String"}
    assert r["messageAttributes"]["blob"]["binaryValue"] == "AQI="


def test_context_values():
    b = Binding(function_name="pii-check", handler="m.h", queue_name="q", memory_mb=256)
    ctx = event.context(b, "ap-northeast-1", "000000000000", 123)
    assert ctx["invoked_function_arn"] == "arn:aws:lambda:ap-northeast-1:000000000000:function:pii-check"
    assert ctx["memory_limit_in_mb"] == "256" and ctx["deadline_ms"] == 123 and ctx["aws_request_id"]
