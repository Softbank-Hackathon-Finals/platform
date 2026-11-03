"""Read account costs and keep missing attribution distinct from measured zero."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from decimal import Decimal
from pathlib import Path

COMPONENTS = ("source", "platform", "target")
POLICY = Path(__file__).resolve().parents[1] / "infra/envs/cost-aws/policy.auto.tfvars.json"


def aws(service, operation, **params):
    args = ["aws", "--region", "us-east-1", service, operation, "--no-paginate",
            "--cli-input-json", json.dumps(params), "--output", "json"]
    result = subprocess.run(args, check=True, capture_output=True, text=True)
    return json.loads(result.stdout)


def pages(call, operation, result_key, **params):
    seen = set()
    while True:
        response = call("ce", operation, **params)
        if result_key not in response or not isinstance(response[result_key], list):
            raise ValueError("Malformed AWS response: " + result_key)
        yield response[result_key]
        token = response.get("NextPageToken")
        if not token:
            return
        if token in seen:
            raise ValueError("Repeated AWS pagination token")
        seen.add(token)
        params["NextPageToken"] = token


def date_range(start, end):
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if first >= last or last > dt.datetime.now(dt.timezone.utc).date():
        raise ValueError("Use start < end <= today (UTC); end is exclusive")
    return {(first + dt.timedelta(days=n)).isoformat() for n in range((last-first).days)}


def aggregate(batches, expected_days, rate):
    totals = {c: None for c in COMPONENTS}
    account_components = {c: None for c in COMPONENTS}
    account_unknown = Decimal(0)
    other = Decimal(0)
    unattributed = Decimal(0)
    unknown = Decimal(0)
    unattributed_groups = unknown_groups = 0
    days, observed_groups = set(), set()
    estimated = False
    for batch in batches:
        for day in batch:
            start = day["TimePeriod"]["Start"]
            if day["TimePeriod"]["End"] != (dt.date.fromisoformat(start) + dt.timedelta(days=1)).isoformat():
                raise ValueError("Expected daily cost intervals")
            days.add(start)
            estimated |= day["Estimated"]
            for group in day["Groups"]:
                keys = group["Keys"]
                if len(keys) != 2 or not keys[0].startswith("project$") or not keys[1].startswith("component$"):
                    raise ValueError("Unexpected cost grouping")
                identity = (start, *keys)
                if identity in observed_groups:
                    raise ValueError("Duplicate cost group across pages")
                observed_groups.add(identity)
                cost = group["Metrics"]["UnblendedCost"]
                amount = Decimal(cost["Amount"])
                if cost["Unit"] != "USD" or not amount.is_finite():
                    raise ValueError("Expected finite USD costs")
                project, component = (key.split("$", 1)[1] for key in keys)
                if component in COMPONENTS:
                    account_components[component] = (account_components[component] or Decimal(0)) + amount
                else:
                    account_unknown += amount
                if not project:
                    unattributed += amount
                    unattributed_groups += 1
                elif project != "bravo-platform":
                    other += amount
                elif component not in COMPONENTS:
                    unknown += amount
                    unknown_groups += 1
                else:
                    totals[component] = (totals[component] or Decimal(0)) + amount
    if days != expected_days:
        raise ValueError("Missing or unexpected daily intervals; do not interpret as zero spend")
    def money(value):
        return {"usd": str(value) if value is not None else None,
                "krw_estimate": str((value * rate).quantize(Decimal("0.01"))) if value is not None else None,
                "observed": value is not None}
    accounted = sum((v for v in totals.values() if v is not None), Decimal(0)) + unknown
    return {"estimated": estimated,
            "account_components": {c: money(v) for c, v in account_components.items()},
            "account_unallocated_component": money(account_unknown), "components": {c: money(v) for c, v in totals.items()},
            "bravo_unknown_component": {**money(unknown), "group_count": unknown_groups},
            "bravo_attributed_total": money(accounted),
            "project_unattributed": {**money(unattributed), "group_count": unattributed_groups},
            "other_projects": money(other),
            "account_total": money(accounted + unattributed + other),
            "observed_group_count": len(observed_groups)}


def report(account, start, end, policy, call=aws):
    days = date_range(start, end)
    if call("sts", "get-caller-identity")["Account"] != account:
        raise ValueError("Wrong AWS account")
    tags = {tag["TagKey"]: tag["Status"] for batch in pages(
        call, "list-cost-allocation-tags", "CostAllocationTags", TagKeys=["project", "component"]
    ) for tag in batch}
    result = aggregate(pages(call, "get-cost-and-usage", "ResultsByTime",
        TimePeriod={"Start": start, "End": end}, Granularity="DAILY", Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "TAG", "Key": "project"}, {"Type": "TAG", "Key": "component"}]),
        days, Decimal(str(policy["krw_per_usd"])))
    result.update({"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                   "period": {"start_inclusive": start, "end_exclusive": end},
                   "scope": "account", "project": "bravo-platform", "metric": "UnblendedCost",
                   "tag_status": {key: tags.get(key, "NOT_AVAILABLE") for key in ("project", "component")},
                   "fx": policy,
                   "attribution_complete": all(tags.get(k) == "Active" for k in ("project", "component"))
                       and result["project_unattributed"]["group_count"] == 0
                       and result["bravo_unknown_component"]["group_count"] == 0,
                   "notes": ["Null component amounts are NOT_OBSERVED, not zero.",
                             "Unattributed groups remain incomplete even when charges and credits net to zero.",
                             "AWS billing and tag activation are delayed; historical costs may remain unattributed.",
                             "Account guard includes other projects; KRW figures are reference-rate estimates."]})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = report(args.account_id, args.start, args.end, json.loads(POLICY.read_text()))
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print("Cost report saved; attribution_complete=" + str(result["attribution_complete"]))


if __name__ == "__main__":
    main()
