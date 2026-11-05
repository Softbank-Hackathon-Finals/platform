import base64
from urllib.parse import quote

import pytest

from rehearsal_runner.exec_output import contains_secret, screen

PLANTED = "pw-9f8e7d6c5b4a"


@pytest.mark.parametrize("prefix", [b"", b"a", b"ab", b"abc", b"abcd"])
@pytest.mark.parametrize("encode", [base64.b64encode, base64.urlsafe_b64encode])
def test_base64_found_at_every_alignment(prefix, encode):
    text = "token: " + encode(prefix + PLANTED.encode() + b"tail").decode()
    assert contains_secret(text, [PLANTED])


def test_raw_and_url_encoded_forms():
    assert contains_secret("x" + PLANTED + "y", [PLANTED])
    assert contains_secret("p=" + quote("a b/" + PLANTED + "&", safe=""), [PLANTED])
    special = "p@ss/w rd+1"
    assert contains_secret("q=" + quote(special, safe=""), [special])
    assert not contains_secret("unrelated text", [PLANTED, ""])


def test_screen_reports_digest_of_whole_output():
    result = screen(b"x" * 3000, lambda: [PLANTED])
    assert result.size == 3000 and result.truncated and len(result.tail) == 2048 and not result.withheld
    assert screen(b"", lambda: [PLANTED]).sha256 is None
