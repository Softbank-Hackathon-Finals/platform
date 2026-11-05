"""Screen rehearsal.exec output before it reaches the model or a record.

Only values the runner itself placed in space A can be found. The whole output
is scanned before the tail is cut, so a value split by the cut is still found.
Any doubt (non-UTF-8, unreadable secret list) withholds the output entirely.
"""

import base64
import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import quote, quote_plus

TAIL_BYTES = 2048


@dataclass(frozen=True)
class Screened:
    tail: str | None
    truncated: bool
    sha256: str | None
    size: int | None
    withheld: bool


def _base64_cores(data: bytes) -> set[str]:
    """Base64 text fully determined by `data` at each of the three byte alignments."""
    cores = set()
    for shift in range(3):
        start = -(-8 * shift // 6)
        end = 8 * (shift + len(data)) // 6
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            text = encode(b"\0" * shift + data + b"\0\0\0").decode("ascii")[start:end]
            if len(text) >= 4:
                cores.add(text)
    return cores


def secret_forms(value: str) -> set[str]:
    data = value.encode("utf-8")
    return {value, quote(value, safe=""), quote_plus(value)} | _base64_cores(data)


def contains_secret(text: str, secrets: Iterable[str]) -> bool:
    for value in secrets:
        if not isinstance(value, str):
            raise TypeError("secret must be text")
        if value and any(form in text for form in secret_forms(value)):
            return True
    return False


def screen(output: bytes, secrets: Callable[[], Iterable[str]]) -> Screened:
    if not output:
        return Screened(None, False, None, None, False)
    digest, size = hashlib.sha256(output).hexdigest(), len(output)
    try:
        text = output.decode("utf-8")
        if contains_secret(text, secrets()):
            return Screened(None, False, digest, size, True)
    except Exception:
        return Screened(None, False, digest, size, True)
    tail = output[-TAIL_BYTES:]
    while tail and tail[0] & 0xC0 == 0x80:  # do not start inside a UTF-8 sequence
        tail = tail[1:]
    return Screened(tail.decode("utf-8"), len(tail) < size, digest, size, False)
