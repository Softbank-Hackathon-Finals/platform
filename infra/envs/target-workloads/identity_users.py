#!/usr/bin/env python3
"""Turn a Cognito user list into a Keycloak partial import. Passwords do not move.

Cognito never exports password hashes. An imported account therefore has no credential at all, and
Keycloak runs required actions only after a successful sign-in: without a password the account can never
sign in, not even to reach UPDATE_PASSWORD. After the import the operator gives every account a temporary
password (README "사용자 옮기기"); the user signs in with it once and must set a new one.

The name stays exactly as Cognito issued it. demo-chat names authors by preferred_username, then
cognito:username, without folding case, so "Visitor01" in Cognito and "visitor01" in Keycloak would be two
authors. Keycloak stores user names in lower case, so a name with an upper-case letter is refused here and
left to the operator to rename on both sides; it is not silently changed.

Input is `aws cognito-idp list-users --user-pool-id <pool>` output on stdin. Output is the JSON body for
Keycloak's partial import. Disabled, unconfirmed and refused accounts are listed on stderr: their names keep
their past messages, so reserve them and do not create them on either side while AUTH_ALSO_TRUST is on.
Nothing here contacts AWS or a cluster, and no secret passes through it.
"""
import json
import sys


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
        if username != username.lower():
            skipped.append({"username": username, "reason": "upper-case name: would become another author"})
            continue
        user = {
            "username": username,
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
    return {"ifResourceExists": "SKIP", "users": users}, skipped


def main():
    result, skipped = convert(json.load(sys.stdin))
    for item in skipped:
        print(f"skipped {item['username']!r}: {item['reason']} (reserve this name)", file=sys.stderr)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
