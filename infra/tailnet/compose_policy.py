#!/usr/bin/env python3
"""Compose the whole tailnet policy from the Git base and the generated fragments (T48, ADR-0035).

    python3 infra/tailnet/compose_policy.py --base infra/tailnet/base-policy.json \
        --identities infra/tailnet/identities.json --tailnet default \
        infra/tailnet/routes-policy.json > policy.json

Top-level lists (grants, tests, ...) are appended in input order without exact duplicates. Maps are
unioned, and a key already present must have exactly the same value: two fragments never widen one
tag's owners or one service's approvers. Each operator identity of this tailnet gets its tag owner
(`tag:bravo-<env>-operator` owned by tailnet admins). The output must not name a person: an email
anywhere is refused, human sources are autogroups or IdP group names only.
"""
import argparse
import json
import re
import sys

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ENV = re.compile(r"^[a-z0-9]([a-z0-9-]{0,40}[a-z0-9])?$")


class PolicyError(ValueError):
    pass


def _merge(into, part, where, top=True):
    for key, value in part.items():
        if key not in into:
            into[key] = value
        elif top and isinstance(into[key], list) and isinstance(value, list):
            into[key] = into[key] + [v for v in value if v not in into[key]]
        elif isinstance(into[key], dict) and isinstance(value, dict):
            _merge(into[key], value, f"{where}.{key}", top=False)
        elif into[key] != value:
            raise PolicyError(f"conflict at {where}.{key}")


def compose(base, fragments, identities, tailnet):
    policy = json.loads(json.dumps(base))
    for fragment in fragments:
        _merge(policy, {k: v for k, v in fragment.items() if k != "tests" or v}, "policy")
    for env, identity in sorted(identities.items()):
        if not ENV.fullmatch(env):
            raise PolicyError(f"bad environment name {env!r}")
        if identity.get("tailnet", "default") == tailnet:
            _merge(policy, {"tagOwners": {f"tag:bravo-{env}-operator": ["autogroup:admin"]}}, "policy")
    if EMAIL.search(json.dumps(policy)):
        raise PolicyError("the policy names a person: use an autogroup or an IdP group")
    return policy


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", required=True)
    parser.add_argument("--identities", required=True)
    parser.add_argument("--tailnet", default="default")
    parser.add_argument("fragments", nargs="*")
    args = parser.parse_args(argv)

    def load(path):
        with open(path) as handle:
            return json.load(handle)

    try:
        policy = compose(load(args.base), [load(f) for f in args.fragments], load(args.identities), args.tailnet)
    except PolicyError as error:
        print(json.dumps({"error": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps(policy, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
