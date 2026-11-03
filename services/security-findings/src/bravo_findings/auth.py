"""T10에서 발급하는 내부 서비스 자격의 검증 경계. 브라우저 로그인 구현이 아니다."""

import hashlib
import hmac
import json
import os
import stat
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SOURCES = {"aws_security_hub", "aws_guardduty", "gcp_scc", "falco", "kyverno", "bravo_controls"}
ID_PATTERN = r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,99}$"


class Credential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    credential_id: str = Field(pattern=ID_PATTERN)
    subject: str = Field(pattern=ID_PATTERN)
    token_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    environments: list[str] = Field(min_length=1, max_length=100)
    permissions: list[Literal["read", "ingest"]] = Field(min_length=1, max_length=1)
    sources: list[str] = Field(default_factory=list, max_length=6)
    not_before: int = Field(ge=0)
    expires_at: int = Field(gt=0)
    revoked: bool = False

    @model_validator(mode="after")
    def validate_scope(self):
        import re

        if any(not re.fullmatch(ID_PATTERN, env) for env in self.environments):
            raise ValueError("invalid environment scope")
        if len(set(self.environments)) != len(self.environments):
            raise ValueError("duplicate environment")
        if not set(self.sources) <= SOURCES or len(set(self.sources)) != len(self.sources):
            raise ValueError("invalid source scope")
        if "ingest" in self.permissions and not self.sources:
            raise ValueError("ingest requires explicit sources")
        if "read" in self.permissions and self.sources:
            raise ValueError("read credentials must not carry ingest source scope")
        if not 0 < self.expires_at - self.not_before <= 86_400_000:
            raise ValueError("credential lifetime must be at most 24 hours")
        return self


class AuthUnavailable(RuntimeError):
    pass


class Authenticator:
    def __init__(self, credentials: list[dict] | None = None, path: str | None = None):
        self._credentials = credentials
        self._path = Path(path) if path else None
        if credentials is not None:
            self._parse(credentials)  # fail at startup for invalid static configuration

    @staticmethod
    def _parse(records: list[dict]) -> list[Credential]:
        if not isinstance(records, list) or len(records) > 1000:
            raise ValueError("invalid credential registry")
        parsed = [Credential.model_validate(r) for r in records]
        for attr in ("credential_id", "token_sha256"):
            if len({getattr(c, attr) for c in parsed}) != len(parsed):
                raise ValueError("duplicate credential")
        return parsed

    def _load(self) -> list[Credential]:
        try:
            if self._credentials is not None:
                return self._parse(self._credentials)
            if self._path is None:
                return []  # deny by default
            fd = os.open(self._path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                    raise ValueError("registry must be a private regular file")
                if info.st_uid not in {0, os.getuid()}:
                    raise ValueError("untrusted registry owner")
                doc = json.loads(stream.read(1_048_577))
                if info.st_size > 1_048_576 or set(doc) != {"version", "credentials"}:
                    raise ValueError("invalid registry")
                if doc["version"] != 1:
                    raise ValueError("unsupported registry")
                return self._parse(doc["credentials"])
        except (OSError, ValueError, TypeError, KeyError, RecursionError):
            # Never include validation input, file contents or token in an exception.
            raise AuthUnavailable("credential registry unavailable") from None

    def authenticate(self, authorization: str | None, now: int) -> Credential | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization[7:]
        if not 32 <= len(token) <= 512 or not token.isascii() or any(c.isspace() for c in token):
            return None
        digest = hashlib.sha256(token.encode("ascii")).hexdigest()
        match = None
        for credential in self._load():
            if hmac.compare_digest(credential.token_sha256, digest):
                match = credential
        if match is None or match.revoked or not match.not_before <= now < match.expires_at:
            return None
        return match
