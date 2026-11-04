"""Pure arithmetic for replication-lag measurements: no database, Docker or clock access."""
import math
import statistics


def percentile(values, q):
    """Nearest-rank percentile, so the result is always a value that was really measured."""
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q / 100 * len(ordered))) - 1]


def summarize(values):
    if not values:
        return {"n": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    return {"n": len(values), "mean": round(statistics.fmean(values), 3),
            **{f"p{q}": round(percentile(values, q), 3) for q in (50, 95, 99)},
            "max": round(max(values), 3)}


def parse_lsn(text):
    if text is None:
        return None
    high, low = text.split("/")
    return (int(high, 16) << 32) | int(low, 16)


def row_lags(acks, seen):
    """Milliseconds from each write's ACK to the first target sample that includes the row.

    acks are (row_id, ack_time) in commit order; seen are (time, highest_visible_id) in time order.
    Returns the lags and how many rows never appeared. A row visible before its ACK returned
    produces a small negative value; keeping it shows the resolution of the method.
    """
    lags, index = [], 0
    for row_id, acked in acks:
        while index < len(seen) and (seen[index][1] is None or seen[index][1] < row_id):
            index += 1
        if index == len(seen):
            return lags, len(acks) - len(lags)
        lags.append((seen[index][0] - acked) * 1000)
    return lags, 0


def first_reach(samples, position):
    """Time of the first (time, position) sample at or past position, or None if never reached."""
    for at, value in samples:
        if value is not None and value >= position:
            return at
    return None


def position_lags(source, target):
    """Milliseconds until the target position passes each new source position, on one clock.

    Only the first sample of each source position counts. Repeats of an unchanged position are
    satisfied at once and would pull the distribution towards zero without any data moving.
    """
    lags, index, previous = [], 0, None
    for at, position in source:
        if position is None or position == previous:
            continue
        previous = position
        while index < len(target) and (target[index][0] < at or target[index][1] is None or target[index][1] < position):
            index += 1
        if index == len(target):
            break
        lags.append((target[index][0] - at) * 1000)
    return lags


def gate_lag_bytes(source, target):
    """The Executor's lag gate: source WAL position minus the reported target position read first."""
    lags, index = [], 0
    for sample in target:
        while index < len(source) and source[index]["t"] < sample["t"]:
            index += 1
        if index == len(source):
            break
        if sample["reported"] is not None and source[index]["wal"] is not None:
            lags.append(source[index]["wal"] - sample["reported"])
    return lags


def share_over_pct(values, limit):
    return round(sum(value > limit for value in values) / len(values) * 100, 2) if values else None


def intervals_ms(times):
    return [(times[index + 1] - times[index]) * 1000 for index in range(len(times) - 1)]


def condense(values):
    """Raw per-run values with median and maximum. An unreached run voids both summary values."""
    unreached = sum(value is None for value in values)
    if unreached:
        return {"raw": values, "median": None, "max": None, "unreached": unreached}
    return {"raw": values, "median": statistics.median(values), "max": max(values)}


def budget(budget_ms, catchup_ms, compare_ms):
    return {"budget_ms": budget_ms, "catchup_ms": catchup_ms, "compare_ms": compare_ms,
            "remaining_for_other_steps_ms": round(budget_ms - catchup_ms - compare_ms, 1)}


def budget_scenarios(conditions, gate_backlog, *, budget_ms, compare_ms, poll_ms):
    """Budget left after catch-up and comparison, by what the cutover waits for.

    A barrier is an ordinary row, so its worst case is the worst row in any run, plus the Executor's
    polling step. The gate scenarios start from the largest backlog the Executor's lag gate admits.
    """
    def worst(metric):
        values = [entry["summary"][metric]["max"] for entry in conditions.values()]
        return None if not values or None in values else max(values)

    def scenario(catchup_ms):
        return None if catchup_ms is None else budget(budget_ms, round(catchup_ms, 1), compare_ms)

    worst_row, gate_barrier, gate_confirmed = worst("steady_visible_lag_max_ms"), None, None
    if gate_backlog:
        timings = gate_backlog["catchup_from_write_stop_ms"]
        gate_barrier = gate_backlog["visible_after_enable_ms"] + poll_ms
        gate_confirmed = gate_backlog["visible_after_enable_ms"] + timings["confirmed_lsn_ms"] - timings["visible_ms"]
    return {"barrier_worst_row": scenario(None if worst_row is None else worst_row + poll_ms),
            "confirmed_lsn_equal": scenario(worst("catchup_confirmed_lsn_ms")),
            "barrier_at_gate_limit": scenario(gate_barrier),
            "confirmed_at_gate_limit": scenario(gate_confirmed)}


def analyze_observation(source, target):
    """Everything that can be derived from reads alone, on the observer's single clock."""
    ids = [sample["max_id"] for sample in source if sample["max_id"] is not None]
    first_seen, highest = [], ids[0] if ids else None
    for sample in source:
        if sample["max_id"] is not None and highest is not None and sample["max_id"] > highest:
            highest = sample["max_id"]
            first_seen.append((highest, sample["t"]))
    lags, missing = row_lags(first_seen, [(sample["t"], sample["max_id"]) for sample in target])
    positions = [(sample["t"], sample["wal"]) for sample in source]
    return {
        # WAL also moves without business writes (checkpoints, autovacuum), so only new rows count.
        "source_idle": not first_seen,
        "new_rows_seen": len(first_seen),
        "rows_not_seen_on_target": missing,
        "row_lag_ms": summarize(lags),
        "received_position_lag_ms": summarize(position_lags(positions, [(s["t"], s["received"]) for s in target])),
        "reported_position_lag_ms": summarize(position_lags(positions, [(s["t"], s["reported"]) for s in target])),
        "gate_lag_bytes": summarize(gate_lag_bytes(source, target)),
        "confirmed_lag_bytes": summarize([s["wal"] - s["confirmed"] for s in source
                                          if s["wal"] is not None and s["confirmed"] is not None]),
    }
