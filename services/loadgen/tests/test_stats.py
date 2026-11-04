"""리포트 · 잠금 측정의 백분위. 창의 p95는 build_window가 계산하고 같은 방법(nearest-rank)이다."""

from bravo_observability import Record, build_window

from bravo_loadgen.stats import nearest_rank


def test_nearest_rank():
    assert nearest_rank([]) is None
    assert nearest_rank([7.0]) == 7.0
    assert nearest_rank([float(i) for i in range(1, 101)]) == 95.0
    assert nearest_rank([float(i) for i in range(1, 21)]) == 19.0
    assert nearest_rank([3.0, 1.0, 2.0], 0.5) == 2.0


def test_report_percentile_agrees_with_the_window_p95():
    """리포트와 창의 p95가 다른 방법이면 같은 실행의 숫자가 어긋난다."""
    latencies = [float(i * 7 % 101) for i in range(1, 300)]
    window = build_window(
        [Record("aws", 200, value) for value in latencies],
        plan_id="p", start="2026-11-08T05:31:10Z", end="2026-11-08T05:31:20Z", pools={"aws": "source"},
    )
    assert window["pools"]["source"]["p95_ms"] == nearest_rank(latencies)
