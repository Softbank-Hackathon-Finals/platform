"""Prepare explicitly mapped human developers; no runtime roles or access keys."""

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ADMIN = "arn:aws:iam::aws:policy/AdministratorAccess"
GROUP = "bravo-human-developers"
PATH = "/team-bravo/humans/"
TAGS = {"project": "softbank-finals", "component": "access", "principal-type": "human"}
READS = {"get-user", "list-users", "list-user-tags", "list-groups-for-user", "list-attached-user-policies",
         "list-attached-group-policies", "get-group", "list-access-keys", "get-login-profile"}
WRITES = {"create-user", "create-group", "attach-group-policy", "add-user-to-group"}


class Failure(RuntimeError):
    pass


class AWS:
    def __init__(self, profile=None):
        self.profile = profile

    def call(self, operation, **params):
        service = "sts" if operation == "get-caller-identity" else "iam"
        if operation not in READS | WRITES | {"get-caller-identity"}:
            raise Failure("operation_not_allowed")
        args = ["aws", service, operation, "--output", "json", "--no-cli-pager", "--cli-connect-timeout", "10", "--cli-read-timeout", "30"]
        if self.profile:
            args += ["--profile", self.profile]
        # No password or key operation is accepted by this client.
        for key, value in params.items():
            args += ["--" + key.replace("_", "-"), json.dumps(value) if isinstance(value, list) else str(value)]
        try:
            result = subprocess.run(args, check=False, capture_output=True, text=True, timeout=60,
                                    env={**os.environ, "AWS_MAX_ATTEMPTS": "1"})
        except subprocess.TimeoutExpired:
            raise Failure("aws_timeout") from None
        if result.returncode:
            match = re.search(r"An error occurred \(([A-Za-z0-9]+)\)", result.stderr)
            raise Failure(match[1] if match else "aws_cli_failed")
        try:
            return json.loads(result.stdout) if result.stdout.strip() else {}
        except ValueError:
            raise Failure("aws_response_invalid") from None


def optional(aws, operation, **params):
    try:
        return aws.call(operation, **params)
    except Failure as error:
        if str(error) == "NoSuchEntity":
            return None
        raise


def validate(config):
    if not re.fullmatch(r"\d{12}", config.get("expected_account", "")):
        raise Failure("expected_account_invalid")
    people = config.get("members", [])
    if not people or len({m["alias"] for m in people}) != len(people):
        raise Failure("roster_invalid")
    if len({m["user_name"].lower() for m in people}) != len(people):
        raise Failure("duplicate_user_mapping")
    if len({m["github_login"].lower() for m in people}) != len(people):
        raise Failure("duplicate_person_mapping")
    for member in people:
        if member.get("principal_type") != "human" or not re.fullmatch(r"[A-Za-z0-9-]{1,39}", member.get("github_login", "")):
            raise Failure("human_identity_required")
        if not re.fullmatch(r"member-[0-9]{2}", member["alias"]):
            raise Failure("public_alias_invalid")
        if not member.get("reuse_existing") and member["user_name"] != "bravo-" + member["github_login"]:
            raise Failure("new_user_name_outside_managed_scope")


def snapshot(aws, config):
    validate(config)
    if aws.call("get-caller-identity").get("Account") != config["expected_account"]:
        raise Failure("account_boundary_mismatch")
    group = optional(aws, "get-group", group_name=GROUP)
    expected_users = {m["user_name"] for m in config["members"]}
    if group and (group["Group"]["Path"] != PATH or any(u["UserName"] not in expected_users for u in group["Users"])):
        raise Failure("group_scope_requires_review")
    group_policies = aws.call("list-attached-group-policies", group_name=GROUP)["AttachedPolicies"] if group else []
    all_users = aws.call("list-users")["Users"]
    # Detect earlier mappings before creating another identity for the same person.
    tags_by_user = {u["UserName"]: {t["Key"]: t["Value"] for t in aws.call("list-user-tags", user_name=u["UserName"])["Tags"]} for u in all_users}
    rows = []
    for member in config["members"]:
        name = member["user_name"]
        duplicates = [u for u, tags in tags_by_user.items() if tags.get("github-login", "").lower() == member["github_login"].lower() and u != name]
        duplicates += [u["UserName"] for u in all_users if u["UserName"].lower() == member["github_login"].lower() and u["UserName"] != name]
        if duplicates:
            raise Failure("existing_identity_mapping_requires_review")
        user = optional(aws, "get-user", user_name=name)
        if not user and member.get("reuse_existing"):
            raise Failure("expected_existing_user_missing")
        if user and not member.get("reuse_existing"):
            tags = tags_by_user[name]
            if user["User"]["Path"] != PATH or any(tags.get(k) != v for k, v in {**TAGS, "github-login": member["github_login"]}.items()):
                raise Failure("existing_user_outside_managed_scope")
        groups = aws.call("list-groups-for-user", user_name=name)["Groups"] if user else []
        policies = aws.call("list-attached-user-policies", user_name=name)["AttachedPolicies"] if user else []
        for item in groups:
            policies += aws.call("list-attached-group-policies", group_name=item["GroupName"])["AttachedPolicies"]
        login = optional(aws, "get-login-profile", user_name=name) if user else None
        keys = aws.call("list-access-keys", user_name=name)["AccessKeyMetadata"] if user else []
        rows.append({"member": member, "exists": user is not None, "admin_attached": any(p["PolicyArn"] == ADMIN for p in policies),
                     "in_group": any(g["GroupName"] == GROUP for g in groups), "key_count": len(keys),
                     "console_profile_exists": login is not None,
                     "password_reset_required": (login or {}).get("LoginProfile", {}).get("PasswordResetRequired"),
                     "permissions_boundary_present": bool((user or {}).get("User", {}).get("PermissionsBoundary"))})
    return {"group_exists": group is not None, "group_admin_attached": any(p["PolicyArn"] == ADMIN for p in group_policies), "members": rows}


def plan(state, console):
    actions = []
    needs_group = any(not r["admin_attached"] for r in state["members"])
    if needs_group and not state["group_exists"]:
        actions.append({"target": "developer-group", "action": "create_group"})
    if needs_group and not state["group_admin_attached"]:
        actions.append({"target": "developer-group", "action": "attach_administrator_access"})
    for row in state["members"]:
        alias = row["member"]["alias"]
        if not row["exists"]:
            actions.append({"target": alias, "action": "create_user"})
        if not row["admin_attached"] and not row["in_group"]:
            actions.append({"target": alias, "action": "add_to_developer_group"})
        if console and not row["member"].get("reuse_existing") and not row["console_profile_exists"]:
            actions.append({"target": alias, "action": "create_initial_console_profile"})
    return actions


def native(helper, config=None):
    args = [str(helper)] + (["--probe"] if config is None else [])
    try:
        result = subprocess.run(args, check=False, input=json.dumps(config) if config else None,
                                capture_output=True, text=True, timeout=180)
        body = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise Failure("keychain_helper_failed") from None
    if result.returncode:
        raise Failure(body.get("error_code", "keychain_helper_failed"))
    return body


def sdk_probe(python):
    if python is None:
        raise Failure("sdk_python_required")
    try:
        result = subprocess.run([str(python), "-I", str(Path(__file__).with_name("create_initial_console_profile.py").resolve()), "--probe"],
                                check=False, capture_output=True, text=True, timeout=30)
        if result.returncode or json.loads(result.stdout).get("sdk_probe") != "PASS":
            raise Failure("sdk_probe_failed")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise Failure("sdk_probe_failed") from None


def apply(aws, config, before, actions, helper=None, sdk_python=None):
    if any(a["action"] == "create_initial_console_profile" for a in actions):
        if helper is None or native(helper).get("keychain_probe") != "PASS":
            raise Failure("keychain_probe_required")
        sdk_probe(sdk_python)
    rows = {r["member"]["alias"]: r for r in before["members"]}
    results = []
    for action in actions:
        kind = action["action"]
        member = rows.get(action["target"], {}).get("member", {})
        name = member.get("user_name")
        outcome = {}
        if kind == "create_group":
            aws.call("create-group", group_name=GROUP, path=PATH)
        elif kind == "attach_administrator_access":
            aws.call("attach-group-policy", group_name=GROUP, policy_arn=ADMIN)
        elif kind == "create_user":
            aws.call("create-user", user_name=name, path=PATH,
                     tags=[{"Key": k, "Value": v} for k, v in {**TAGS, "github-login": member["github_login"]}.items()])
        elif kind == "add_to_developer_group":
            aws.call("add-user-to-group", group_name=GROUP, user_name=name)
        elif kind == "create_initial_console_profile":
            outcome = native(helper, {"expected_account": config["expected_account"], "user_name": name, "profile": config.get("profile"),
                                      "sdk_python": str(sdk_python) if sdk_python else None,
                                      "sdk_script": str(Path(__file__).with_name("create_initial_console_profile.py").resolve())})
        else:
            raise Failure("unknown_action")
        results.append({**action, "result": "applied", **outcome})
    return results


def receipt(before, after, actions, mode):
    before_keys = {r["member"]["alias"]: r["key_count"] for r in before["members"]}
    rows = [{"member": r["member"]["alias"], "reused_existing": bool(r["member"].get("reuse_existing")),
             "user_exists": r["exists"], "administrator_access_policy_attached": r["admin_attached"],
             "permissions_boundary_present": r["permissions_boundary_present"],
             "console_profile_exists": r["console_profile_exists"], "password_reset_required": r["password_reset_required"],
             "new_access_key_count": r["key_count"] - before_keys[r["member"]["alias"]], "human_login_verified": False} for r in after["members"]]
    return {"schema_version": 1, "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": mode, "account_boundary_matches": True, "policy": "AdministratorAccess",
            "scope": "human_development_only", "actions": actions, "members": rows,
            "runtime_roles_changed": False, "external_messages_sent": False,
            "effective_access_and_human_login_verified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["plan", "apply", "verify"])
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--with-console", action="store_true")
    parser.add_argument("--keychain-helper", type=Path)
    parser.add_argument("--sdk-python", type=Path, help="isolated Python environment containing boto3")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        config = json.loads(args.config.read_text())
        aws = AWS(config.get("profile"))
        before = snapshot(aws, config)
        actions = plan(before, args.with_console)
        if args.command == "plan":
            result = {"mode": "plan", "account_boundary_matches": True, "actions": actions}
        elif args.command == "verify":
            result = receipt(before, before, [], "readback")
        else:
            applied = apply(aws, config, before, actions, args.keychain_helper, args.sdk_python)
            after = snapshot(aws, config)
            result = receipt(before, after, applied, "apply_readback")
            if any(not r["user_exists"] or not r["administrator_access_policy_attached"] or r["new_access_key_count"] != 0 for r in result["members"]):
                raise Failure("readback_incomplete_requery_before_retry")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (Failure, OSError, ValueError, KeyError) as error:
        # Generic parser/filesystem failures must not print private input values.
        code = str(error) if isinstance(error, Failure) else "local_input_or_execution_failed"
        print(json.dumps({"error_code": code, "mutation_result": "requery_before_retry"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
