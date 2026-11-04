"""Render the private RKE2 authenticator and single-user RBAC bindings from a verified roster."""
import argparse
import json
import re
from datetime import datetime
from pathlib import Path

AUDIENCE = "bravo-platform-rke2-human"
USERNAME_PREFIX = "bravo:oidc:"
AWS_CLAIMS = "claims['https://sts.amazonaws.com/']"


def render(config):
    account, role, store = (config[key] for key in ("account", "role_arn", "store_arn"))
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("Invalid AWS account")
    path = r"aws-reserved/sso.amazonaws.com/(?:[a-z0-9-]+/)?"
    if not re.fullmatch(rf"arn:aws:iam::{account}:role/{path}AWSReservedSSO_BravoDevelopmentAdministrator_[a-fA-F0-9]{{16}}", role):
        raise ValueError("Expected the provisioned team SSO role ARN")
    if not re.fullmatch(rf"arn:aws:identitystore::{account}:identitystore/d-[0-9a-f]{{10}}", store):
        raise ValueError("Expected the team's Identity Store ARN")
    issuer = config["issuer"]
    if not re.fullmatch(r"https://[a-z0-9-]+\.tokens\.sts\.global\.api\.aws", issuer):
        raise ValueError("Expected the issuer reported by AWS IAM")
    deadline = datetime.fromisoformat(config["access_until"])
    if deadline.utcoffset() is None:
        raise ValueError("An explicit time zone is required")
    members = config["members"]
    uid_pattern = r"(?:[a-f0-9]{10}-)?[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}"
    if not members or len(set(members.values())) != len(members):
        raise ValueError("Expected one distinct Identity Store user ID per member")
    bindings = []
    for alias, user_id in sorted(members.items()):
        if not re.fullmatch(r"member-0[1-5]", alias) or not re.fullmatch(uid_pattern, user_id):
            raise ValueError("Invalid roster entry")
        bindings.append({
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding",
            "metadata": {"name": "bravo-human-oidc-" + alias},
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": "cluster-admin"},
            "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": USERNAME_PREFIX + user_id}],
        })
    rules = [
        (f"claims.sub == '{role}'", "Expected the team SSO role"),
        (f"{AWS_CLAIMS}.aws_account == '{account}' && {AWS_CLAIMS}.identity_store_arn == '{store}'",
         "Expected the team account and directory"),
        (f"{AWS_CLAIMS}.identity_store_user_id in {json.dumps(sorted(members.values()))}", "Expected an assigned human"),
        ("claims.exp > claims.iat && claims.exp - claims.iat <= 300", "Expected a short-lived token"),
        (f"claims.exp <= {int(deadline.timestamp())}", "Token would outlive the access window"),
        (f"claims.aud == '{AUDIENCE}' || claims.aud == ['{AUDIENCE}']", "Expected only the RKE2 human audience"),
    ]
    auth = {"apiVersion": "apiserver.config.k8s.io/v1", "kind": "AuthenticationConfiguration", "jwt": [{
        "issuer": {"url": issuer, "audiences": [AUDIENCE]},
        "claimValidationRules": [{"expression": expr, "message": message} for expr, message in rules],
        "claimMappings": {"username": {"expression": f"'{USERNAME_PREFIX}' + {AWS_CLAIMS}.identity_store_user_id"}},
    }]}
    return auth, {"apiVersion": "v1", "kind": "List", "items": bindings}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", choices=("authenticator", "bindings"))
    args = parser.parse_args()
    auth, bindings = render(json.loads(args.config.read_text()))
    print(json.dumps(auth if args.output == "authenticator" else bindings, indent=2))
