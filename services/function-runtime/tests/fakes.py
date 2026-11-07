from bravo_function_runtime.queue import Message


class FakeQueue:
    def __init__(self, name="q", bodies=()):
        self.name = name
        self.pending = [self._msg(i, b) for i, b in enumerate(bodies)]
        self.deleted: list[str] = []

    @staticmethod
    def _msg(i, body):
        return Message(message_id=f"m{i}", receipt_handle=f"r{i}", body=body,
                       attributes={"ApproximateReceiveCount": "1", "SentTimestamp": "1700000000000",
                                   "SenderId": "AIDA", "ApproximateFirstReceiveTimestamp": "1700000000001",
                                   "Unrelated": "x"},
                       message_attributes={"kind": {"DataType": "String", "StringValue": "message.created"},
                                           "blob": {"DataType": "Binary", "BinaryValue": b"\x01\x02"}},
                       md5_of_body="md5")

    def arn(self):
        return f"arn:aws:sqs:ap-northeast-1:000000000000:{self.name}"

    def attributes(self):
        return {"VisibilityTimeout": "30", "FifoQueue": "false"}

    def receive(self, max_messages, wait_s):
        out, self.pending = self.pending[:max_messages], self.pending[max_messages:]
        return out

    def delete(self, messages):
        self.deleted += [m.message_id for m in messages]
