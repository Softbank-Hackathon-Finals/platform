#!/usr/bin/env python3
"""Turn a Cognito user list into a Keycloak partial import, without passwords.

Cognito never exports password hashes, so accounts move and passwords do not: every imported user must
set a new password on first sign-in (UPDATE_PASSWORD). The name a user signed in with stays the same,
because demo-chat names authors by preferred_username, then cognito:username: the Cognito username becomes
the Keycloak username and both issuers produce the same author.

Input is `aws cognito-idp list-users --user-pool-id <pool>` output on stdin. Output is the JSON body for
Keycloak's partial import (Realm settings > Action > Partial import, or kcadm create partialImport).
Nothing here contacts AWS or a cluster, and no secret passes through it.
"""
import json
import sys

ATTRIBUTES = ("email", "given_name", "family_name")


def convert(listing):
    users, skipped = [], []
    for entry in listing.get("Users", []):
        username = entry.get("Username")
        attributes = {a["Name"]: a["Value"] for a in entry.get("Attributes", [])}
        # A disabled account or one that never confirmed stays behind; the operator decides on it explicitly.
        if not username or not entry.get("Enabled", False) or entry.get("UserStatus") not in (
                "CONFIRMED", "FORCE_CHANGE_PASSWORD", "RESET_REQUIRED"):
            skipped.append({"username": username, "reason": "disabled or unconfirmed"})
            continue
        user = {
            "username": username.lower(),  # source-identity's pool is case-insensitive; Keycloak stores lowercase
            "enabled": True,
            "requiredActions": ["UPDATE_PASSWORD"],
            "attributes": {"cognito_sub": [attributes["sub"]]} if "sub" in attributes else {},
        }
        if "email" in attributes:
            user["email"] = attributes["email"]
            user["emailVerified"] = attributes.get("email_verified") == "true"
        if "given_name" in attributes:
            user["firstName"] = attributes["given_name"]
        if "family_name" in attributes:
            user["lastName"] = attributes["family_name"]
        users.append(user)
    names = [user["username"] for user in users]
    if len(names) != len(set(names)):
        raise ValueError("two Cognito users collapse to the same lowercase username")
    return {"ifResourceExists": "SKIP", "users": users}, skipped


def main():
    result, skipped = convert(json.load(sys.stdin))
    for item in skipped:
        print(f"skipped {item['username']!r}: {item['reason']}", file=sys.stderr)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
