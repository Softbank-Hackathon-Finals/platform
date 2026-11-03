"""T1 read-only AWS access inventory; never issues or exports credentials."""

import argparse
import json
import os
import re
import subprocess
from datetime import datetime, timezone

READS = {
    ("sts", "get-caller-identity"),
    ("sso-admin", "list-instances"),
    ("sso-admin", "list-permission-sets"),
    ("sso-admin", "list-account-assignments"),
    ("eks", "list-clusters"),
    ("eks", "describe-cluster"),
    ("eks", "list-access-entries"),
}


class ReadError(RuntimeError):
    """Only a non-identifying error code may leave the AWS command boundary."""


class AWS:
    def __init__(self, profile=None):
        self.profile = profile

    def read(self, service, operation, region, **params):
        if (service, operation) not in READS:
            raise ValueError("operation_not_allowed")
        command = ["aws", service, operation, "--region", region,
                   "--output", "json", "--no-cli-pager"]
        if self.profile:
            command += ["--profile", self.profile]
        for key, value in params.items():
            command += ["--" + key.replace("_", "-"), str(value)]
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=45)
        except subprocess.TimeoutExpired:
            raise ReadError("timeout") from None
        except OSError:
            raise ReadError("aws_cli_unavailable") from None
        if result.returncode:
            match = re.search(r"An error occurred \(([A-Za-z0-9]+)\)", result.stderr)
            # AWS errors can contain account IDs, role ARNs and signed URLs.
            raise ReadError(match[1] if match else "aws_cli_error")
        try:
            data = json.loads(result.stdout)
        except ValueError:
            raise ReadError("invalid_json") from None
        if not isinstance(data, dict):
            raise ReadError("invalid_response")
        return data


def principal_type(arn):
    if ":assumed-role/AWSReservedSSO_" in arn:
        return "sso_role"
    if ":assumed-role/" in arn:
        return "assumed_role"
    if ":user/" in arn:
        return "iam_user"
    if arn.endswith(":root"):
        return "root"
    return "unknown"


def inspect(aws, expected_account, region, identity_center_region, cluster=None, role=None):
    if not re.fullmatch(r"[0-9]{12}", expected_account):
        raise ValueError("expected_account_must_have_12_digits")
    if role and not re.fullmatch(r"arn:aws:iam::" + expected_account + r":role/[A-Za-z0-9+=,.@_/-]+", role):
        raise ValueError("principal_must_be_an_iam_role_in_expected_account")
    checks = []

    def record(name, state, **facts):
        checks.append({"check": name, "state": state, **facts})

    def query(check_name, service, operation, target_region, **params):
        try:
            return aws.read(service, operation, target_region, **params)
        except ReadError as error:
            record(check_name, "unknown", error_code=str(error))
            return None

    identity = query("caller", "sts", "get-caller-identity", region)
    if identity is None:
        return report(checks, region, identity_center_region)
    if identity.get("Account") != expected_account:
        record("account_boundary", "mismatch")
        return report(checks, region, identity_center_region)
    record("account_boundary", "observed", matches_expected=True)
    kind = principal_type(identity.get("Arn", ""))
    record("caller", "observed", principal_type=kind)
    record("operator_sso_session", "observed" if kind == "sso_role" else "not_verified")

    instances = query("identity_center", "sso-admin", "list-instances", identity_center_region)
    if instances is not None:
        rows = instances.get("Instances")
        if not isinstance(rows, list):
            record("identity_center", "unknown", error_code="missing_instances_field")
        else:
            record("identity_center", "observed" if rows else "missing", visible_instances=len(rows))
            # Do not select an arbitrary instance in a multi-instance response.
            if len(rows) == 1 and rows[0].get("InstanceArn"):
                instance = rows[0]["InstanceArn"]
                sets = query("permission_sets", "sso-admin", "list-permission-sets",
                             identity_center_region, instance_arn=instance)
                if sets is not None:
                    names = sets.get("PermissionSets")
                    if not isinstance(names, list):
                        record("permission_sets", "unknown", error_code="missing_permission_sets_field")
                    else:
                        record("permission_sets", "observed" if names else "missing", count=len(names))
                        assignment_count = 0
                        complete = True
                        for permission_set in names:
                            assignments = query("account_assignments", "sso-admin", "list-account-assignments",
                                                identity_center_region, instance_arn=instance,
                                                account_id=expected_account, permission_set_arn=permission_set)
                            if assignments is None:
                                complete = False
                            elif not isinstance(assignments.get("AccountAssignments"), list):
                                record("account_assignments", "unknown", error_code="missing_assignments_field")
                                complete = False
                            else:
                                assignment_count += len(assignments["AccountAssignments"])
                        if complete:
                            record("account_assignments", "observed" if assignment_count else "missing",
                                   count=assignment_count, intended_team_membership_verified=False)
            elif len(rows) > 1:
                record("permission_sets", "not_checked", reason="multiple_instances_require_selection")
            elif rows:
                record("permission_sets", "unknown", error_code="missing_instance_arn")

    clusters = query("eks_inventory", "eks", "list-clusters", region)
    if clusters is not None:
        rows = clusters.get("clusters")
        if not isinstance(rows, list):
            record("eks_inventory", "unknown", error_code="missing_clusters_field")
        else:
            record("eks_inventory", "observed" if rows else "missing", visible_clusters=len(rows))
    if not cluster:
        record("source_cluster", "not_checked", reason="source_cluster_not_selected")
    else:
        response = query("source_cluster", "eks", "describe-cluster", region, name=cluster)
        if response is not None:
            data = response.get("cluster") or {}
            mode = data.get("accessConfig", {}).get("authenticationMode")
            active = data.get("status") == "ACTIVE"
            supported = mode in ("API", "API_AND_CONFIG_MAP")
            record("source_cluster", "observed" if active and supported else "not_ready",
                   active=active, access_entries_supported=supported)
            if supported and role:
                entries = query("source_role_entry", "eks", "list-access-entries", region, cluster_name=cluster)
                if entries is not None:
                    values = entries.get("accessEntries")
                    if not isinstance(values, list):
                        record("source_role_entry", "unknown", error_code="missing_access_entries_field")
                    else:
                        record("source_role_entry", "observed" if role in values else "missing",
                               policy_scope_and_rbac_verified=False)
            elif not role:
                record("source_role_entry", "not_checked", reason="principal_role_not_selected")
    return report(checks, region, identity_center_region)


def report(checks, region, identity_center_region):
    return {
        "schema_version": 1,
        "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": "read_only_inventory",
        "scope": {"aws_region": region, "identity_center_region": identity_center_region},
        "checks": checks,
        "team_access": "NOT_VERIFIED",
        "remaining_operational_checks": [
            "intended_user_and_group_assignments",
            "each_user_sso_login",
            "eks_endpoint_reachability_and_allow_deny_rbac",
            "rke2_individual_identity_and_allow_deny_rbac",
            "executor_database_and_pgbouncer_authorization",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", required=True)
    parser.add_argument("--identity-center-region", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--expected-account", default=os.environ.get("BRAVO_EXPECTED_AWS_ACCOUNT"))
    parser.add_argument("--source-cluster")
    parser.add_argument("--principal-role")
    args = parser.parse_args()
    if not args.expected_account:
        parser.error("set BRAVO_EXPECTED_AWS_ACCOUNT or --expected-account from reviewed local deployment inputs")
    try:
        result = inspect(AWS(args.profile), args.expected_account, args.region,
                         args.identity_center_region, args.source_cluster, args.principal_role)
    except ValueError as error:
        print(json.dumps({"error_code": str(error)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(c["state"] == "observed" for c in result["checks"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
