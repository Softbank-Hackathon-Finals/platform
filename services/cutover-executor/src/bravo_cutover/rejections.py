"""원격 조작 없이 거부한 요청. code는 API 오류 코드, http_status는 응답 코드다."""


class Rejected(Exception):
    code = "rejected"
    http_status = 409


class IdempotencyConflict(Rejected):
    code = "idempotency_key_conflict"


class StaleEpoch(Rejected):
    code = "stale_epoch"


class LeaseHeld(Rejected):
    code = "lease_held"


class DeadlinePassed(Rejected):
    code = "deadline_passed"
    http_status = 422


class UnknownEnvironment(Rejected):
    code = "unknown_environment"
    http_status = 422


class ConnectionRefMismatch(Rejected):
    code = "connection_ref_mismatch"
    http_status = 422


class SameResource(Rejected):
    code = "same_resource"
    http_status = 422
