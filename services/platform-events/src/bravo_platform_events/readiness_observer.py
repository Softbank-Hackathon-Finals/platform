"""T19 readiness observation, run inside the operator approval host (t15-rehearsal-host).

That Pod holds the Executor command credential (operator_ports config). operations/gate/run.py `capture`
runs this through `kubectl exec`: it asks the Executor to observe the registered pair for the server
readiness policy (POST /v1/readiness) and prints the observation as one JSON line. Nothing is recorded
here; the gate relay packages it for the plan's current context and records it.

The Executor may raise the target sequence (raise only, target closed); see bravo_cutover.readiness.
"""

import json
import ssl
import sys
from pathlib import Path

from .executor import ExecutorClient, ExecutorReadError
from .gate_readiness import POLICY
from .operator_ports import CONFIG


def request_for(pair, policy=POLICY):
    """The Executor request for the registered pair (forward direction) and the server policy's scope."""
    return {"source_environment_id": pair["source"]["environment_id"],
            "target_environment_id": pair["target"]["environment_id"],
            "tables": list(policy.tables), "sequence_columns": [list(p) for p in policy.sequence_columns],
            "max_age_seconds": policy.max_age_seconds}


def named_by_pair(observation, pair):
    """Refuse an observation that does not name both DBs by the registered pair's connection refs.

    T10 compares these refs with the cutover command before SHIFT and cutover approval
    (PLAN_GATE_DATABASE_PAIR_MISMATCH). Checking here stops a mismatch before the relay records and
    signs it for the context, instead of after the GATE has spent it.
    """
    o = observation if isinstance(observation, dict) else {}
    replication = o.get("replication") if isinstance(o.get("replication"), dict) else {}
    for side in ("source", "target"):
        part = o.get(side) if isinstance(o.get(side), dict) else {}
        expected = pair[side]
        if (part.get("environment_id") != expected["environment_id"]
                or part.get("database_ref") != expected["connection_ref"]
                or replication.get(side + "_database_ref") != expected["connection_ref"]):
            raise ExecutorReadError("readiness_pair_mismatch")
    return observation


def observe(config_file=CONFIG, *, client=ExecutorClient):
    config = json.loads(Path(config_file).read_text())
    executor = config.get("executor") if isinstance(config, dict) else None
    if (type(executor) is not dict
            or set(executor) != {"url", "ca_file", "token_file", "command_token_file", "registered_pair"}
            or not executor["url"].startswith("https://")):
        raise ValueError("operator_ports_configuration_invalid")
    with client(executor["url"], token=Path(executor["token_file"]).read_text().strip(),
                command_token=Path(executor["command_token_file"]).read_text().strip(),
                tls_context=ssl.create_default_context(cafile=executor["ca_file"]),
                # The Executor may take its whole 30 s capture window (two sites, schema read twice);
                # a shorter client wait would report a hold while the Executor still raises the sequence.
                timeout_seconds=30) as executor_client:
        executor_client.verified_bindings(executor["registered_pair"])  # the deployed pair is the registered one
        observation = executor_client.observe_readiness(request_for(executor["registered_pair"]))
    return named_by_pair(observation, executor["registered_pair"])


def main():
    try:
        print(json.dumps({"status": "PASS", "observation": observe()}, sort_keys=True))
    except ExecutorReadError as error:
        print(json.dumps({"status": "HOLD", "code": str(error)}))
        raise SystemExit(10) from None
    except Exception as error:  # noqa: BLE001 - the operator reads one bounded code, never a trace
        print(json.dumps({"status": "FAIL", "reason": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    sys.exit(main())
