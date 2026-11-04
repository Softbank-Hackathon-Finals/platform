"""리포트에 쓰는 백분위. 창의 p95와 같은 방법(nearest-rank)이다."""

import math
from collections.abc import Sequence


def nearest_rank(values: Sequence[float], fraction: float = 0.95) -> float | None:
    if not values:
        return None
    return sorted(values)[math.ceil(fraction * len(values)) - 1]
