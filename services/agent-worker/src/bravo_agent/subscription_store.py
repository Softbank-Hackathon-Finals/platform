"""One designated VM owns renewal; only the broker sees AWS and refresh credentials."""

import fcntl
import json
import math
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import boto3
from botocore.config import Config

from .subscription_auth import (
    ISSUER,
    SCOPES,
    AuthFailure,
    ReauthenticationRequired,
    cleared_record,
)


def credential_policy(secret_arn: str):
    if not re.fullmatch(r"arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+", secret_arn):
        raise AuthFailure("EXACT_SECRET_REQUIRED")
    return {"Version": "2012-10-17", "Statement": [{"Sid": "OwnSubscriptionSession",
        "Effect": "Allow", "Action": ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue",
                                      "secretsmanager:UpdateSecretVersionStage"], "Resource": secret_arn}]}


def private_directory(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AuthFailure("PRIVATE_DIRECTORY_REQUIRED")


def host_id(directory: Path) -> str:
    private_directory(directory)
    path = directory / "host-id"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as stream:
            stream.write(uuid.uuid4().urn + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (info.st_uid != os.getuid() or info.st_mode & 0o077
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise AuthFailure("PRIVATE_HOST_ID_REQUIRED")
        value = stream.read(100).strip()
    if not value.startswith("urn:uuid:") or uuid.UUID(value).version != 4:
        raise AuthFailure("INVALID_HOST_ID")
    return value


@contextmanager
def broker_lock(directory: Path):
    private_directory(directory)
    fd = os.open(directory / "session.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (info.st_uid != os.getuid() or info.st_mode & 0o077
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise AuthFailure("PRIVATE_LOCK_REQUIRED")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AuthFailure("SUBSCRIPTION_BROKER_BUSY") from None
        yield
    finally:
        os.close(fd)


class SubscriptionStore:
    def __init__(self, secret_arn: str, runtime_host_id: str, client=None):
        match = re.fullmatch(r"arn:aws:secretsmanager:([a-z0-9-]+):[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+", secret_arn)
        if not match or not runtime_host_id.startswith("urn:uuid:") or uuid.UUID(runtime_host_id).version != 4:
            raise AuthFailure("EXACT_SECRET_AND_HOST_REQUIRED")
        self.arn, self.host_id = secret_arn, runtime_host_id
        self.client = client or boto3.client("secretsmanager", region_name=match[1],
            config=Config(retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=20))

    def read(self):
        response = self.client.get_secret_value(SecretId=self.arn, VersionStage="AWSCURRENT")
        raw = response["SecretString"]
        if len(raw.encode()) > 64000:
            raise AuthFailure("INVALID_CREDENTIAL_RECORD")
        record = json.loads(raw)
        if (not isinstance(record, dict) or record.get("schema_version") != 1
                or record.get("runtime_host_id") != self.host_id
                or record.get("state") not in {"UNCONFIGURED", "ACTIVE", "SIGNED_OUT", "REAUTH_REQUIRED", "PLAN_DISABLED"}):
            raise AuthFailure("CREDENTIAL_HOST_MISMATCH")
        if record["state"] != "UNCONFIGURED":
            if (record.get("issuer") != ISSUER or not isinstance(record.get("subject"), str)
                    or not record["subject"] or not str(record.get("client_id", "")).startswith("oaiapp_")):
                raise AuthFailure("INVALID_ADMIN_BINDING")
        if record["state"] == "ACTIVE":
            if (not SCOPES <= set(record.get("scopes", []))
                    or any(not isinstance(record.get(k), str) or not record[k]
                           for k in ("access_token", "refresh_token", "id_token"))
                    or type(record.get("expires_at")) not in (int, float)
                    or type(record.get("earliest_refresh_at")) not in (int, float)
                    or not math.isfinite(record["expires_at"])
                    or not math.isfinite(record["earliest_refresh_at"])):
                raise AuthFailure("INVALID_CREDENTIAL_RECORD")
        return record, response["VersionId"]

    def replace(self, record, previous_version):
        """Stage a full token set, then compare-and-move AWSCURRENT atomically."""
        if record.get("runtime_host_id") != self.host_id:
            raise AuthFailure("CREDENTIAL_HOST_MISMATCH")
        version = str(uuid.uuid4())
        stage = "bravo-pending-" + version
        self.client.put_secret_value(SecretId=self.arn, ClientRequestToken=version,
                                     SecretString=json.dumps(record), VersionStages=[stage])
        # AWS rejects a changed RemoveFromVersionId. Preserve the pending version for
        # operator recovery on failure; never overwrite a concurrent registration.
        self.client.update_secret_version_stage(SecretId=self.arn, VersionStage="AWSCURRENT",
                                                 MoveToVersionId=version,
                                                 RemoveFromVersionId=previous_version)
        self.client.update_secret_version_stage(SecretId=self.arn, VersionStage=stage,
                                                 RemoveFromVersionId=version)
        return version

    def access_token(self, oauth):
        """Caller must hold the designated VM's lock for read/refresh/child execution."""
        record, version = self.read()
        if record["state"] != "ACTIVE":
            raise ReauthenticationRequired("SUBSCRIPTION_REAUTH_REQUIRED")
        if record["expires_at"] - time.time() <= 300:
            if time.time() < record["earliest_refresh_at"]:
                raise AuthFailure("REFRESH_NOT_YET_AVAILABLE")
            try:
                renewed = oauth.refresh(record)
            except ReauthenticationRequired:
                self.replace(cleared_record(record, "REAUTH_REQUIRED"), version)
                raise
            self.replace(renewed, version)
            if renewed["state"] != "ACTIVE":
                raise ReauthenticationRequired("CHATGPT_PLAN_PERMISSION_MISSING")
            record = renewed
        return record["access_token"]
