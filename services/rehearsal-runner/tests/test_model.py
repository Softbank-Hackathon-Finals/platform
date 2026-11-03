from rehearsal_runner.isolation.model import Kind, Result, Status, exit_code


def r(kind: Kind, status: Status, required: bool = True) -> Result:
    return Result("X", "t", kind, status, required=required)


def test_all_pass_is_zero():
    assert exit_code([r(Kind.CONTROL, Status.PASS), r(Kind.ISOLATION, Status.PASS)]) == 0


def test_no_results_is_untestable():
    assert exit_code([]) == 2


def test_failed_control_is_untestable_even_if_isolation_failed():
    # 대조가 깨졌으면 격리 결과도 믿지 않는다
    assert exit_code([r(Kind.CONTROL, Status.FAIL), r(Kind.ISOLATION, Status.FAIL)]) == 2
    assert exit_code([r(Kind.CONTROL, Status.INCONCLUSIVE)]) == 2


def test_isolation_failure_is_breach():
    assert exit_code([r(Kind.CONTROL, Status.PASS), r(Kind.ISOLATION, Status.FAIL)]) == 1
    assert exit_code([r(Kind.CONTROL, Status.PASS), r(Kind.ISOLATION, Status.FAIL, False)]) == 1


def test_inconclusive_required_isolation_is_untestable_but_optional_is_ignored():
    control = r(Kind.CONTROL, Status.PASS)
    assert exit_code([control, r(Kind.ISOLATION, Status.INCONCLUSIVE)]) == 2
    assert exit_code([control, r(Kind.ISOLATION, Status.INCONCLUSIVE, False)]) == 0


def test_info_never_changes_the_verdict():
    assert exit_code([r(Kind.CONTROL, Status.PASS), r(Kind.INFO, Status.FAIL)]) == 0
