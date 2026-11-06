"""Read the subscription metrics the way CNPG's exporter does, and condense repeated trials. No Docker, no clock."""
import re
import statistics

METRIC_PREFIX = "cnpg_pg_subscription_"
SAMPLE = re.compile(r'^(\w+)\{subname="([^"]*)"\} (\S+)$')
TYPE = re.compile(r"^# TYPE (\w+) (\w+)$")


def parse_exposition(text):
    """Subscription samples and types from an exporter's text page; every other metric is ignored."""
    samples, types = {}, {}
    for line in text.splitlines():
        typed = TYPE.match(line)
        if typed and typed.group(1).startswith(METRIC_PREFIX):
            types[typed.group(1)] = typed.group(2)
        sample = SAMPLE.match(line)
        if sample and sample.group(1).startswith(METRIC_PREFIX):
            samples.setdefault(sample.group(2), {})[sample.group(1).removeprefix(METRIC_PREFIX)] = float(sample.group(3))
    return {"samples": samples, "types": types}


def run_query(execute, query):
    """One scrape: rows keyed by the LABEL column, valued by the metric columns, in the configured order.

    CNPG drops every row when the number of columns and of configured metrics differ, so this refuses instead.
    """
    columns, rows = execute(query["query"])
    usages = {name: mapping["usage"] for item in query["metrics"] for name, mapping in item.items()}
    if columns != list(usages):
        raise ValueError(f"query returns {columns}, configured metrics are {list(usages)}")
    label = next(name for name, usage in usages.items() if usage == "LABEL")
    return {row[columns.index(label)]: {name: float(row[columns.index(name)]) for name in columns if name != label} for row in rows}


def condense(trials):
    """One row per setting and failure, in first-seen order. Raw trials stay beside it."""
    groups = {}
    for trial in trials:
        groups.setdefault((trial["disable_on_error"], trial["failure"]), []).append(trial)
    rows = []
    for (disable_on_error, failure), runs in groups.items():
        flowing = [run["rows_flowing_after_heal_s"] for run in runs if run["rows_flowing_after_heal_s"] is not None]
        errors = [run["apply_errors_added"] for run in runs]
        rows.append({"disable_on_error": disable_on_error, "failure": failure, "runs": len(runs),
                     "stopped": sum(run["outcome"] == "stopped" for run in runs),
                     "recovered": sum(run["outcome"] == "recovered" for run in runs),
                     "neither": sum(run["outcome"] not in ("stopped", "recovered") for run in runs),
                     "enabled_during": sorted({run["enabled_lowest"] for run in runs}),
                     "workers_lowest": sorted({run["workers_lowest"] for run in runs}),
                     "apply_errors_added": {"min": min(errors), "median": statistics.median(errors), "max": max(errors)},
                     "rows_flowing_after_heal_s": ({"median": statistics.median(flowing), "max": max(flowing)} if flowing else None)})
    return rows
