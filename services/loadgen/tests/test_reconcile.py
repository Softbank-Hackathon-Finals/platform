"""전수 대조: 일부러 넣은 실패 · 유실 · 중복을 정확히 세는지 본다."""

import copy
import uuid

import pytest

from bravo_loadgen.journal import message_author, message_body
from bravo_loadgen.reconcile import reconcile, write_gap

RUN = "t18-test"


def key(n):
    return str(uuid.UUID(int=n))


def attempt(n, no=1, *, status=201, outcome="http", acked=True, final=True, message_id=None,
            finished=None):
    logical = key(n)
    return {
        "type": "attempt", "kind": "write", "logical_id": logical,
        "attempt_id": f"{logical}.{no}", "attempt_no": no, "outcome": outcome,
        "status": status, "acked": acked, "final": final,
        "message_id": (n if message_id is None else message_id) if acked else None,
        "sent_at_s": float(n), "finished_at_s": float(n) if finished is None else finished,
    }


def row(n, *, id=None, idempotency_key=None, author=None, body=None):
    return {
        "id": n if id is None else id,
        "idempotency_key": idempotency_key or key(n),
        "author": author or message_author(RUN),
        "body": body or message_body(RUN, key(n)),
    }


def journal(*attempts, scheduled=None, ended=True, author=None):
    logical = {a["logical_id"] for a in attempts if a["kind"] == "write"}
    start = {"type": "run_start", "run_id": RUN}
    if author:
        start["author"] = author
    records = [start, *attempts]
    if ended:
        records.append({
            "type": "run_end", "ended_at_s": 100.0,
            "scheduled_writes": len(logical) if scheduled is None else scheduled,
            "scheduled_reads": 0,
        })
    return records


def counts(result):
    return {k: v for k, v in result["counts"].items() if v}


def test_clean_run_passes():
    result = reconcile(journal(attempt(1), attempt(2)), [row(1), row(2)])
    assert result["verdict"] == "PASS" and result["reasons"] == []
    assert counts(result) == {"logical_writes": 2, "attempts": 2, "confirmed": 2}
    assert result["nfr2"] == {"failed": 0, "lost": 0, "duplicated": 0}


def test_retry_with_one_row_is_confirmed_not_duplicated():
    records = journal(
        attempt(1, 1, status=503, acked=False, final=False),
        attempt(1, 2, outcome="timeout", status=None, acked=False, final=False),
        attempt(1, 3, status=200),
    )
    result = reconcile(records, [row(1)])
    assert result["verdict"] == "PASS"
    assert counts(result) == {
        "logical_writes": 1, "attempts": 3, "confirmed": 1, "retried": 1, "idempotent_replays": 1,
    }
    assert result["max_attempts"] == 3


def test_acked_write_missing_from_db_is_lost():
    result = reconcile(journal(attempt(1), attempt(2)), [row(1)])
    assert result["verdict"] == "FAIL" and "LOST" in result["reasons"]
    assert result["nfr2"] == {"failed": 0, "lost": 1, "duplicated": 0}
    assert result["samples"]["lost"] == [key(2)]


def test_same_body_under_another_key_is_duplicated():
    """UNIQUE를 우회한 복사본: 멱등 키가 달라도 본문의 요청 ID로 잡는다."""
    copy_row = row(1, id=9, idempotency_key=str(uuid.uuid4()))
    result = reconcile(journal(attempt(1)), [row(1), copy_row])
    assert result["verdict"] == "FAIL" and "DUPLICATED" in result["reasons"]
    assert result["nfr2"]["duplicated"] == 1
    assert result["counts"]["duplicate_extra_rows"] == 1


def test_same_key_twice_is_duplicated():
    """두 DB의 기록을 합친 경우처럼 같은 키의 행이 둘이면 중복이다."""
    result = reconcile(journal(attempt(1)), [row(1), row(1, id=5)])
    assert "DUPLICATED" in result["reasons"]


def test_never_acked_and_not_stored_is_failed():
    records = journal(attempt(1, status=503, acked=False))
    result = reconcile(records, [])
    assert result["verdict"] == "FAIL" and "FAILED" in result["reasons"]
    assert result["counts"]["failed_not_stored"] == 1
    assert result["nfr2"] == {"failed": 1, "lost": 0, "duplicated": 0}


def test_never_acked_but_stored_is_failed_and_separated():
    """확인 응답은 끝내 못 받았지만 저장된 경우. 유실이 아니지만 클라이언트에겐 실패다."""
    records = journal(attempt(1, outcome="timeout", status=None, acked=False))
    result = reconcile(records, [row(1)])
    assert result["counts"]["failed_but_stored"] == 1
    assert result["nfr2"] == {"failed": 1, "lost": 0, "duplicated": 0}
    assert result["samples"]["failed_but_stored"] == [key(1)]


def test_integrity_conflict_is_failed():
    records = journal(attempt(1, status=409, acked=False))
    result = reconcile(records, [row(1)])
    assert result["counts"]["integrity_conflict"] == 1
    assert "INTEGRITY_CONFLICT" in result["reasons"]
    assert result["nfr2"]["failed"] == 1


def test_row_never_requested_is_phantom():
    stray = row(99, idempotency_key=str(uuid.uuid4()), body=f"t18 run={RUN} req={uuid.uuid4()}")
    result = reconcile(journal(attempt(1)), [row(1), stray])
    assert result["verdict"] == "FAIL" and "PHANTOM" in result["reasons"]
    assert result["counts"]["phantom_rows"] == 1


def test_changed_body_is_content_mismatch():
    result = reconcile(journal(attempt(1)), [row(1, body="tampered")])
    assert result["verdict"] == "FAIL" and "CONTENT_MISMATCH" in result["reasons"]
    assert result["counts"]["content_mismatch"] == 1


def test_signed_in_run_expects_the_token_name_as_author():
    """로그인을 켠 실행(T47)은 작성자가 계정 이름이다. 그 이름이어야 내용이 같다."""
    signed = journal(attempt(1), attempt(2), author="loadgen")
    result = reconcile(signed, [row(1, author="loadgen"), row(2)])
    assert counts(result) == {"logical_writes": 2, "attempts": 2, "confirmed": 1, "content_mismatch": 1}
    assert result["samples"]["content_mismatch"] == [key(2)]  # 실행별 이름으로 들어간 쪽이 불일치다


def test_changed_author_is_content_mismatch():
    result = reconcile(journal(attempt(1)), [row(1, author="someone")])
    assert result["counts"]["content_mismatch"] == 1


def test_acked_id_differing_from_the_row_is_content_mismatch():
    """응답이 가리킨 메시지와 DB의 메시지가 다르면 다른 DB가 응답했을 수 있다."""
    result = reconcile(journal(attempt(1, message_id=500)), [row(1)])
    assert result["counts"]["content_mismatch"] == 1


def test_lone_copy_under_another_key_is_content_mismatch():
    """원본은 없고 다른 키의 복사본만 있으면 저장된 것이 아니다."""
    result = reconcile(journal(attempt(1)), [row(1, idempotency_key=str(uuid.uuid4()))])
    assert result["verdict"] == "FAIL" and result["counts"]["content_mismatch"] == 1


def test_each_defect_is_counted_once_in_its_own_bucket():
    records = journal(
        attempt(1),  # 정상
        attempt(2),  # 유실
        attempt(3),  # 중복
        attempt(4, status=503, acked=False),  # 실패
        attempt(5, status=409, acked=False),  # 충돌
    )
    rows = [row(1), row(3), row(3, id=30, idempotency_key=str(uuid.uuid4())), row(5)]
    result = reconcile(records, rows)
    assert result["counts"]["confirmed"] == 1
    assert result["nfr2"] == {"failed": 2, "lost": 1, "duplicated": 1}
    total = sum(
        result["counts"][k]
        for k in ("confirmed", "lost", "duplicated", "failed_not_stored", "failed_but_stored",
                  "integrity_conflict", "content_mismatch")
    )
    assert total == result["counts"]["logical_writes"] == 5


def test_samples_are_bounded():
    records = journal(*[attempt(n) for n in range(1, 40)])
    result = reconcile(records, [])
    assert result["counts"]["lost"] == 39 and len(result["samples"]["lost"]) == 20


def test_reads_are_not_part_of_the_write_reconciliation():
    read = {"type": "attempt", "kind": "read", "logical_id": "r", "attempt_id": "r.1", "attempt_no": 1,
            "outcome": "http", "status": 200, "acked": True, "final": True, "message_id": None,
            "sent_at_s": 0.0, "finished_at_s": 0.0}
    result = reconcile(journal(attempt(1), read), [row(1)])
    assert result["counts"]["logical_writes"] == 1 and result["verdict"] == "PASS"


# --- 없으면 0이 아니라 판정 불가 ---


def test_empty_run_is_unknown_not_pass():
    result = reconcile(journal(), [])
    assert result["verdict"] == "UNKNOWN" and result["reasons"] == ["NO_WRITES"]


def test_missing_run_end_is_unknown():
    """부하가 중간에 죽은 기록은 통과로 읽지 않는다."""
    result = reconcile(journal(attempt(1), ended=False), [row(1)])
    assert result["verdict"] == "UNKNOWN" and "JOURNAL_INCOMPLETE" in result["reasons"]


def test_scheduled_count_mismatch_is_unknown():
    result = reconcile(journal(attempt(1), scheduled=2), [row(1)])
    assert result["verdict"] == "UNKNOWN" and "JOURNAL_INCOMPLETE" in result["reasons"]


def test_logical_write_without_final_attempt_is_unknown():
    result = reconcile(journal(attempt(1, final=False, status=503, acked=False)), [])
    assert result["verdict"] != "PASS" and "JOURNAL_INCOMPLETE" in result["reasons"]


def test_fail_outranks_unknown():
    result = reconcile(journal(attempt(1), attempt(2), ended=False), [row(1)])
    assert result["verdict"] == "FAIL"
    assert {"LOST", "JOURNAL_INCOMPLETE"} <= set(result["reasons"])


def test_input_is_not_mutated():
    records = journal(attempt(1))
    rows = [row(1)]
    before = copy.deepcopy((records, rows))
    reconcile(records, rows)
    assert (records, rows) == before


# --- 쓰기 응답 사이 최대 간격 ---


def acks(*times):
    return [attempt(i + 1, finished=t) for i, t in enumerate(times)]


def test_gap_between_successful_responses():
    gap = write_gap(acks(1.0, 2.0, 7.5, 8.0), load_end_s=8.0)
    assert gap == {"max_ms": 5500.0, "from_s": 2.0, "to_s": 7.5, "position": "between", "acks": 4}


def test_refused_writes_do_not_close_the_gap():
    """쓰기 차단 중 503이 빠르게 와도 쓰기가 멈춘 것이다. 성공 응답끼리만 잰다."""
    records = [
        attempt(1, finished=1.0),
        attempt(2, status=503, acked=False, final=False, finished=2.0),
        attempt(3, status=503, acked=False, final=False, finished=3.0),
        attempt(4, finished=6.0),
    ]
    assert write_gap(records, load_end_s=6.0)["max_ms"] == 5000.0


def test_gap_from_load_start_to_first_ack_counts():
    gap = write_gap(acks(4.0, 4.5), load_end_s=4.5)
    assert (gap["max_ms"], gap["position"]) == (4000.0, "lead")


def test_gap_from_last_ack_to_load_end_counts():
    """끝까지 쓰기가 멈춘 채 부하가 끝난 경우를 놓치지 않는다."""
    gap = write_gap(acks(0.5, 1.0), load_end_s=10.0)
    assert (gap["max_ms"], gap["position"]) == (9000.0, "tail")


def test_no_ack_at_all_spans_the_whole_load():
    gap = write_gap([attempt(1, status=503, acked=False)], load_end_s=10.0)
    assert gap == {"max_ms": 10000.0, "from_s": 0.0, "to_s": 10.0, "position": "no_ack", "acks": 0}


def test_acks_after_the_load_ended_do_not_stretch_the_gap():
    """부하가 끝난 뒤의 재시도 응답은 쓰기 공백이 아니다. 끊긴 구간은 부하 끝까지만 잰다."""
    gap = write_gap(acks(1.0, 2.0, 25.0), load_end_s=10.0)
    assert (gap["max_ms"], gap["position"]) == (8000.0, "tail")


def test_reads_do_not_count_as_write_responses():
    read = {"type": "attempt", "kind": "read", "acked": True, "finished_at_s": 5.0}
    gap = write_gap([*acks(1.0, 9.0), read], load_end_s=9.0)
    assert gap["max_ms"] == 8000.0


@pytest.mark.parametrize("bad", [0, -1])
def test_gap_requires_a_positive_load_end(bad):
    with pytest.raises(ValueError):
        write_gap([], load_end_s=bad)
