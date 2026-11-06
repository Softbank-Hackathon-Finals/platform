#!/usr/bin/env python3
"""Run existing Bravo diagnostic suites with personal auth and recoverable ownership receipts."""

import argparse
import fcntl
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from team_runtime.journal import Failure, kubectl, locked, save
from team_runtime.recipes import (
    REPO,
    credentials,
    isolation,
    preflight,
    read_environment,
)
from team_runtime.recovery import cleanup, inspect


def now():
    return datetime.now(UTC).isoformat()


def activate(root, *, write=True):
    """Point this process at the run's kubectl shim. Only the lock holder writes the shim
    files (atomically), so a concurrent `status` never rewrites a running execution's bin/."""
    executable = shutil.which("kubectl", path=os.environ.get("BRAVO_ORIGINAL_PATH", os.environ["PATH"]))
    if not executable:
        raise Failure("kubectl_missing")
    os.environ.setdefault("BRAVO_ORIGINAL_PATH", os.environ["PATH"])
    os.environ["BRAVO_REAL_KUBECTL"] = executable
    os.environ["BRAVO_RUN_ROOT"] = str(root)
    directory = root / "bin"
    if write:
        directory.mkdir(exist_ok=True)
        bridge = directory / "kubectl_bridge.py"
        _replace(bridge, "import sys\nsys.path.insert(0, " + repr(str(REPO / "scripts"))
                 + ")\nfrom team_runtime.journal import shim\nraise SystemExit(shim(sys.argv[1:]))\n", 0o600)
        _replace(directory / "kubectl", "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " "
                 + shlex.quote(str(bridge)) + ' "$@"\n', 0o700)
    os.environ["PATH"] = str(directory) + os.pathsep + os.environ["BRAVO_ORIGINAL_PATH"]


def _replace(path, text, mode):
    temporary = path.with_name("." + path.name + ".tmp")
    temporary.write_text(text)
    temporary.chmod(mode)
    os.replace(temporary, path)


def identity():
    result = kubectl(["auth", "whoami", "-o", "json"])
    if result.returncode:
        raise Failure("identity_unavailable")
    return json.loads(result.stdout)["status"]["userInfo"]["username"]


def summary(root):
    record = json.loads((root / "journal.json").read_text())
    return {
        "run_id": record["run_id"],
        "suite": record["suite"],
        "execution": record["execution"],
        "verdict": record.get("verdict", "UNCONFIRMED"),
        "cleanup": record.get("cleanup", "UNCONFIRMED"),
        "error": record.get("error"),
        "receipt": str(root / "journal.json"),
    }


def execute(args):
    subject = identity()
    if args.command in ("status", "cleanup"):
        if not args.run_id or not __import__("re").fullmatch(
            r"team-[a-f0-9]{32}", args.run_id
        ):
            raise Failure("run_id_required")
        root = args.state / args.run_id
        record = json.loads((root / "journal.json").read_text())
        config = json.loads((root / "environment.json").read_text())
        if (
            record["subject"] != subject
            or record["cluster_uid"] != config["cluster_uid"]
        ):
            raise Failure("run_owner_or_cluster_mismatch")
    else:
        config, provenance = read_environment(
            args.environment_namespace, args.environment
        )
    cluster = kubectl(["get", "namespace", "kube-system", "-o", "json"])
    if (
        cluster.returncode
        or json.loads(cluster.stdout)["metadata"]["uid"] != config["cluster_uid"]
    ):
        raise Failure("cluster_identity_mismatch")
    if args.command == "check":
        print(json.dumps(preflight(config, True)))
        return 0
    if args.command == "run":
        preflight(config, args.suite == "credentials")
        root = args.state / ("team-" + uuid4().hex)
        root.mkdir(parents=True, mode=0o700)
        save(root / "environment.json", config)
        save(
            root / "journal.json",
            {
                "schema": "bravo.team-run/v1",
                "run_id": root.name,
                "suite": args.suite,
                "subject": subject,
                "cluster_uid": config["cluster_uid"],
                "environment": provenance,
                "started": now(),
                "execution": "STARTING",
                "resources": [],
            },
        )
        print(
            json.dumps({"run_id": root.name, "receipt": str(root / "journal.json")}),
            flush=True,
        )
    activate(root, write=False)
    with (root / "execution.lock").open("a") as lease:
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if args.command == "status":
                inspect(root)
                print(json.dumps(summary(root)), flush=True)
                return 0
            raise Failure("run_still_active") from None
        activate(root)  # lock holder only (#466 review)
        if args.command != "run":
            with locked(root) as value:
                if value["execution"] in ("STARTING", "RUNNING"):
                    value["execution"] = "INTERRUPTED"
        operation_failed = False
        try:
            if args.command == "run":
                with locked(root) as value:
                    value["execution"] = "RUNNING"
                    value["executor_pid"] = os.getpid()
                    value["executor_group"] = os.getpgrp()
                if args.suite == "isolation":
                    verdict, extra = isolation(root, config), {}
                else:
                    verdict, extra = credentials(root, config)
                with locked(root) as value:
                    value.update(
                        execution="FINISHED", verdict=verdict, finished=now(), **extra
                    )
            if args.command in ("run", "cleanup"):
                with locked(root) as value:
                    value["cleanup"] = "UNCONFIRMED"
                cleanup(root, config)
            else:
                inspect(root)
                with locked(root) as value:
                    if value["execution"] in ("STARTING", "RUNNING"):
                        value["execution"] = "INTERRUPTED"
        except (Exception, KeyboardInterrupt) as error:  # noqa: BLE001 — persist an interrupted diagnostic without exposing provider text.
            operation_failed = True
            category = (
                str(error) if isinstance(error, Failure) else type(error).__name__
            )
            with locked(root) as value:
                value["error"] = category
                value["cleanup"] = "UNCONFIRMED"
                if value["execution"] in ("STARTING", "RUNNING"):
                    value["execution"] = "INTERRUPTED"
            if args.command == "run":
                try:
                    cleanup(root, config)
                except (Exception, KeyboardInterrupt) as cleanup_error:  # noqa: BLE001 — cleanup cannot erase the original failure.
                    with locked(root) as value:
                        value["cleanup_error"] = (
                            str(cleanup_error)
                            if isinstance(cleanup_error, Failure)
                            else type(cleanup_error).__name__
                        )
            # A hard kill or expired credential leaves the durable intent for a fresh
            # personal-auth cleanup command; never renew or replay the diagnostic.
        result = summary(root)
        print(json.dumps(result), flush=True)
        if operation_failed:
            return 1
        if args.command == "status":
            return 0
        if args.command == "cleanup":
            return 0 if result["cleanup"] == "CLEAN" else 1
        return 0 if result["verdict"] == "PASS" and result["cleanup"] == "CLEAN" else 1


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "objects":
        return subprocess.run([sys.executable, str(REPO / "scripts/object_operations.py"), *sys.argv[2:]], check=False).returncode
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("check", "run", "status", "cleanup"))
    p.add_argument("--suite", choices=("isolation", "credentials"), default="isolation")
    p.add_argument(
        "--access",
        type=Path,
        required=True,
        help="Existing personal team-test.json, contains no token",
    )
    p.add_argument(
        "--state", type=Path, default=Path.home() / ".local/state/bravo-team-runs"
    )
    p.add_argument("--run-id")
    p.add_argument("--environment", default="bravo-self-service-runtime")
    p.add_argument("--environment-namespace", default="bravo-team-access")
    p.add_argument(
        "--team-tool",
        type=Path,
        default=REPO / "infra/envs/rehearsal-access/team_test.py",
    )
    p.add_argument("--authenticated", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args()
    os.umask(0o077)
    args.state = args.state.expanduser().resolve()
    if args.authenticated:
        if not os.environ.get("BRAVO_TEAM_DIRECT_TOKEN"):
            raise Failure("personal_auth_wrapper_required")
        signal.signal(
            signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())
        )
        return execute(args)
    if not args.team_tool.is_file() or not args.access.is_file():
        raise Failure("team_access_bundle_missing")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        str(REPO / path)
        for path in (
            "services/rehearsal-runner/src",
            "services/agent-worker/src",
            "services/platform-events/src",
        )
    )
    command = [
        sys.executable,
        str(args.team_tool.resolve()),
        "--profile",
        str(args.access.resolve()),
        "--",
        "uv",
        "run",
        "--project",
        str(REPO / "services/platform-events"),
        "--frozen",
        "--no-default-groups",
        "--with",
        "pydantic",
        "--with",
        "pyjwt",
        "--with",
        "cryptography",
        "--with",
        "pyyaml",
        "python",
        str(Path(__file__).resolve()),
        *sys.argv[1:],
        "--authenticated",
    ]
    return subprocess.run(command, env=env, check=False).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Failure, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(
            json.dumps(
                {
                    "error": str(error)
                    if isinstance(error, Failure)
                    else type(error).__name__,
                    "replayed": False,
                }
            ),
            file=sys.stderr,
        )
        sys.exit(2)
