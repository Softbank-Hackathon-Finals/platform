import pytest

from bravo_gate.contract import GateError, decode


@pytest.mark.parametrize(
    "raw",
    [
        b'{"decision":"BLOCK","decision":"PASS"}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b'{"x":"\\ud800"}',
        b"[]",
        b"{" * 30,
        b" " + b" " * 1048576,
    ],
)
def test_ambiguous_or_unbounded_json_has_no_authority(raw):
    with pytest.raises(GateError):
        decode(raw)
