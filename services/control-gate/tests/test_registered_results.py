from copy import deepcopy
from datetime import timedelta

import pytest

from bravo_gate.contract import GateError, canonical
from bravo_gate.models import RunSnapshot


@pytest.mark.parametrize("field", ["question", "order", "evaluated_at"])
def test_all_document_fields_must_match_registered_result(service, field):
    app, _ = service
    report = deepcopy(app.evaluate("actor", "context"))
    if field == "question":
        report["rows"][0]["question"] = "changed question"
    elif field == "order":
        report["rows"].reverse()
    else:
        report["evaluated_at"] = "2026-11-04T11:34:59Z"
    with pytest.raises(GateError, match="GATE_RESULT_MISMATCH"):
        app.require_security("actor", "context", canonical(report))


@pytest.mark.parametrize("mode", ["missing", "unavailable", "unauthenticated"])
def test_registered_result_is_required_on_consumption(service, mode):
    app, state = service
    report = app.evaluate("actor", "context")
    if mode == "missing":
        state["reports"].clear()
    elif mode == "unavailable":
        def offline(*_):
            raise RuntimeError("secret database url must never escape")
        app.report_provider = offline
    else:
        app.authenticate_report = lambda *_: False
    with pytest.raises(GateError, match="^GATE_RESULT_(NOT_FOUND|UNAVAILABLE)$"):
        app.require_security("actor", "context", canonical(report))


def test_result_replacement_between_reads_blocks(service):
    app, state = service
    report = app.evaluate("actor", "context")
    record = state["reports"][report["gate_id"]]
    altered = deepcopy(report)
    altered["rows"][0]["question"] = "changed"
    records = iter((record, RunSnapshot(record.ref, canonical(altered))))
    app.report_provider = lambda *_: next(records)
    with pytest.raises(GateError, match="GATE_RESULT_MISMATCH"):
        app.require_security("actor", "context", canonical(report))


def test_slow_result_lookup_cannot_extend_security_deadline(service):
    app, state = service
    report = app.evaluate("actor", "context")
    def slow(*_):
        state["now"] += timedelta(seconds=61)
        return state["reports"][report["gate_id"]]
    app.report_provider = slow
    with pytest.raises(GateError):
        app.require_security("actor", "context", canonical(report))
