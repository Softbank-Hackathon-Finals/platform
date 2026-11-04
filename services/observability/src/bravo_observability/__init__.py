"""T36 판정용 관측 계약의 기준 구현. 계약은 docs/observability/README.md."""

from bravo_observability.sli import Record, build_window, judge, receive

__all__ = ["Record", "build_window", "judge", "receive"]
