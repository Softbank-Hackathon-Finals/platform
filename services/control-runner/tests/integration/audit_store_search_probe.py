"""verify_audit_store_search.sh가 컨테이너 안에서 부르는 단계.

실제 저장소에서 ObjectStoreAuditSearch를 돌린다. 정상이면 present, 보존 기한이 지난 사본 · 관리자 덮어쓰기 · 삭제 표시는
변조 흔적(예외)이어야 한다. 키는 환경 변수로만 받고 출력하지 않는다.
"""

import json
import os
import secrets
import sys
import time
from datetime import UTC, datetime, timedelta

import botocore.session
from botocore.config import Config

from bravo_controls.audit_probes import AuditSearchProbe, CanaryReceipt
from bravo_controls.audit_store import ObjectStoreAuditSearch
from bravo_controls.otlp_audit import AuditOrigin
from bravo_controls.runner import Environment, RunContext, TestSpec

ORIGIN = AuditOrigin(
    "target",
    "sha256:" + "c" * 64,
    "22222222-2222-4222-8222-222222222222",
    "11111111-1111-4111-8111-111111111111",
    "target-dev",
    "target-db-1",
)
BUCKET, PREFIX = "bravo-audit", "audit/onprem"


def s3(access, secret, **config):
    return botocore.session.get_session().create_client(
        "s3",
        region_name="us-east-1",
        endpoint_url="http://s3:8333",
        aws_access_key_id=os.environ[access],
        aws_secret_access_key=os.environ[secret],
        config=Config(s3={"addressing_style": "path"}, **config),
    )


def main(phase):
    reader = s3("RK", "RS", connect_timeout=2, read_timeout=3, signature_version="s3v4",
                retries={"mode": "standard", "total_max_attempts": 1})
    admin = s3("AK", "SK")
    state = json.load(open("/w/state.json"))
    start = datetime.fromisoformat(state["start"])
    ctx = RunContext("run-1", "demo-chat", Environment("target", "target", "onprem:rke2/test"))
    receipt = CanaryReceipt(ctx, "AUD-01.1", state["marker"], "postgres", "chat", "target-dev", "bravo_test",
                            start, start + timedelta(seconds=1), start + timedelta(minutes=10), frozenset({"login"}))
    if phase != "normal":
        stored = admin.list_object_versions(Bucket=BUCKET, Prefix=PREFIX + "/")["Versions"][0]
        if phase == "expired-retention":
            # Same export again under a new uuidv7 key, retained for only a few seconds, then let it lapse
            ms = int(time.time() * 1000)
            raw = f"{ms:012x}7{secrets.token_hex(2)[1:]}{(8 | secrets.randbelow(4)):x}{secrets.token_hex(8)[1:]}"
            copy = stored["Key"].rsplit("/", 1)[0] + f"/logs_{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}.json"
            body = admin.get_object(Bucket=BUCKET, Key=stored["Key"])["Body"].read()
            admin.put_object(Bucket=BUCKET, Key=copy, Body=body, ObjectLockMode="COMPLIANCE",
                             ObjectLockRetainUntilDate=datetime.now(UTC) + timedelta(seconds=3))
            time.sleep(6)
        elif phase == "tamper-overwrite":
            admin.put_object(Bucket=BUCKET, Key=stored["Key"], Body=b"{}")
        else:
            admin.delete_object(Bucket=BUCKET, Key=stored["Key"])
    search = ObjectStoreAuditSearch(reader, bucket=BUCKET, prefix=PREFIX, region="us-east-1", origin=ORIGIN)
    out = {}
    try:
        batch = search(receipt, min(datetime.now(UTC), receipt.deadline))
        out["events"] = len(batch.events)
        out["ingested_after_event"] = all(e.ingested_at >= e.timestamp for e in batch.events)
        probe = AuditSearchProbe(receipt_provider=lambda *_: receipt, search=search,
                                 source_binding=search.source_binding, streams=ORIGIN.streams,
                                 evidence_sink=lambda _: None)
        out["observed"] = probe(ctx, TestSpec("AUD-01.1", "AUD-01", "1", "present", "not_applicable")).observed
    except Exception as error:  # 결과로 기록하고 셸 쪽이 기대값과 대조한다
        out["error"] = f"{type(error).__name__}: {error}"
    print(json.dumps(out))


if __name__ == "__main__":
    main(sys.argv[1])
