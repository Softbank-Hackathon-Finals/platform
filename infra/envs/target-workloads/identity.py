#!/usr/bin/env python3
"""Render the target sign-in server (Keycloak) that replaces the source's Cognito user pool.

demo-chat knows only a standard OIDC issuer and a public client ID, so moving its sign-in off Cognito
is a settings change: AUTH_ISSUER points here. This renders the server and one realm with one public
PKCE client. It creates no users and holds no passwords: the admin Secret and the demo accounts come
from the operator (README "목적지 로그인 서버"). The load generator's own client is defined here too
(loadgen_client) but registered by the operator, not imported. Nothing here contacts a cluster.
"""
import argparse
import json
import re

from render import LABELS, database, resource

# Keycloak 26.0.7 release index, so each node runs its own platform's child:
# linux/arm64 ec690174 (the target node since 2026-11-06), linux/amd64 32d2eb0f.
IMAGE = "quay.io/keycloak/keycloak:26.0.7@sha256:4388e2379b7e870a447adbe7b80bd61f5fbf04e925832b19669fda4957f05a81"
SELECTOR = {"app.kubernetes.io/name": "target-login"}
REALM = "bravo"
CLIENT_ID = "demo-chat"
LOADGEN_CLIENT_ID = "demo-chat-loadgen"
ADMIN_SECRET = "target-login-admin"
# T17 renders the Cloudflare tunnel with this label; the public login hostname reaches Keycloak only through it.
EDGE = {"app": "bravo-edge-tunnel"}
HTTPS_ORIGIN = re.compile(r"^https://[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+(:[0-9]{1,5})?$")


def origin(value, what):
    if not isinstance(value, str) or not HTTPS_ORIGIN.match(value):
        raise ValueError(f"{what} must be https://host[:port] without a path")
    return value


def issuer(public_url):
    """demo-chat's AUTH_ISSUER for this server."""
    return f"{origin(public_url, 'public URL')}/realms/{REALM}"


def _attribute(name, display, validations, viewers=("admin", "user")):
    # Only the admin edits: demo-chat names authors from these claims, so a user must not rename themselves.
    return {"name": name, "displayName": display, "validations": validations, "multivalued": False,
            "permissions": {"view": list(viewers), "edit": ["admin"]}}


# Keycloak 26's default profile requires email, first and last name, so a moved account without them would be
# stopped at VERIFY_PROFILE and could type any unverified address. Here none is required and none is user-editable.
USER_PROFILE = {
    "attributes": [
        _attribute("username", "${username}", {"length": {"min": 3, "max": 255},
                                                "username-prohibited-characters": {},
                                                "up-username-not-idn-homograph": {}}),
        _attribute("email", "${email}", {"email": {}, "length": {"max": 255}}),
        _attribute("firstName", "${firstName}", {"length": {"max": 255}, "person-name-prohibited-characters": {}}),
        _attribute("lastName", "${lastName}", {"length": {"max": 255}, "person-name-prohibited-characters": {}}),
        # Kept from Cognito for tracing a moved account. No token mapper: demo-chat never sees it.
        _attribute("cognito_sub", "Cognito sub", {"length": {"max": 255}}, viewers=("admin",)),
    ],
    "groups": [],
}


def realm(app_origin):
    """One realm, one public client, no users. Sign-up is off: the operator creates demo accounts."""
    app = origin(app_origin, "app origin")
    return {
        "realm": REALM, "enabled": True, "sslRequired": "external",
        "registrationAllowed": False, "resetPasswordAllowed": False, "rememberMe": False,
        # Sign-in is by user name only. Addresses are not verified here, so they must not be a login key.
        "loginWithEmailAllowed": False, "duplicateEmailsAllowed": False,
        "editUsernameAllowed": False,
        "bruteForceProtected": True,
        "components": {"org.keycloak.userprofile.UserProfileProvider": [{
            "providerId": "declarative-user-profile", "subComponents": {},
            "config": {"kc.user.profile.config": [json.dumps(USER_PROFILE, sort_keys=True)]},
        }]},
        # The app accepts ID tokens only; keep them as short as Cognito's (source-identity: 60 minutes).
        "accessTokenLifespan": 3600,
        "clients": [{
            "clientId": CLIENT_ID, "name": "demo-chat", "enabled": True, "protocol": "openid-connect",
            "publicClient": True, "standardFlowEnabled": True,
            "directAccessGrantsEnabled": False, "implicitFlowEnabled": False,
            "serviceAccountsEnabled": False, "frontchannelLogout": True,
            "redirectUris": [f"{app}/"], "webOrigins": [app],
            "attributes": {"pkce.code.challenge.method": "S256", "post.logout.redirect.uris": f"{app}/"},
        }],
    }


def loadgen_client():
    """The load generator's (T18) client, kept apart so the browser client never takes a password.

    Confidential: a token needs this client's secret as well as the account's password. No browser flow and no
    redirect target. fullScopeAllowed is off, so its access token carries no account-console roles. No refresh
    token and no optional scope, so no offline token either: one would outlive a password change and a user
    logout, and the generator signs in again with the password anyway. It is not in the realm import:
    --import-realm never touches an existing realm, and the secret must not sit in the rendered ConfigMap.
    The operator registers it through the admin API and Keycloak makes the secret (README).
    """
    return {
        "clientId": LOADGEN_CLIENT_ID, "name": "demo-chat load generator", "enabled": True,
        "protocol": "openid-connect", "publicClient": False, "clientAuthenticatorType": "client-secret",
        "standardFlowEnabled": False, "implicitFlowEnabled": False, "directAccessGrantsEnabled": True,
        "serviceAccountsEnabled": False, "frontchannelLogout": False, "fullScopeAllowed": False,
        "redirectUris": [], "webOrigins": [],
        # Keycloak 26.0.7 drops the realm's default and optional scopes alike once either list is given, so
        # both are written out. An empty optional list alone leaves the ID token without preferred_username.
        # basic carries sub, profile preferred_username.
        "defaultClientScopes": ["basic", "profile"], "optionalClientScopes": [],
        "attributes": {"use.refresh.tokens": "false"},
    }


def identity(node_name, public_url, app_origin):
    # Use the database renderer's exact-node validation and retained local PV shape, like objects.py.
    volume = database(node_name)[2]
    volume["metadata"]["name"] = "bravo-target-dev-login"
    volume["spec"]["capacity"] = {"storage": "1Gi"}
    volume["spec"]["local"]["path"] = "/var/lib/bravo-target-dev/login"
    volume["spec"]["claimRef"] = {"namespace": "target-dev", "name": "target-login"}
    claim = resource("v1", "PersistentVolumeClaim", "target-login", spec={
        "accessModes": ["ReadWriteOnce"], "storageClassName": "bravo-target-local",
        "volumeName": "bravo-target-dev-login",
        "resources": {"requests": {"storage": "1Gi"}},
    })
    config = resource("v1", "ConfigMap", "target-login-realm", data={
        f"{REALM}-realm.json": json.dumps(realm(app_origin), indent=2, sort_keys=True),
    })
    secret_env = [{"name": name, "valueFrom": {"secretKeyRef": {"name": ADMIN_SECRET, "key": key}}}
                  for name, key in (("KC_BOOTSTRAP_ADMIN_USERNAME", "username"),
                                    ("KC_BOOTSTRAP_ADMIN_PASSWORD", "password"))]
    service = resource("v1", "Service", "target-login", spec={
        "selector": SELECTOR, "ports": [{"name": "http", "port": 8080, "targetPort": "http"}],
    })
    deploy = resource("apps/v1", "Deployment", "target-login", spec={
        "replicas": 1, "strategy": {"type": "Recreate"},
        "selector": {"matchLabels": SELECTOR},
        "template": {"metadata": {"labels": {**LABELS, **SELECTOR}}, "spec": {
            "automountServiceAccountToken": False,
            "serviceAccountName": "target-login",
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000,
                                "fsGroup": 1000, "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "keycloak", "image": IMAGE,
                # --import-realm skips a realm that already exists, so a restart keeps the operator's accounts.
                "args": ["start", "--import-realm"],
                "env": [
                    # One instance on a retained local file store. No second database to run beside the
                    # migrated one; HA is not claimed, as for the target DB and object store.
                    {"name": "KC_DB", "value": "dev-file"},
                    {"name": "KC_CACHE", "value": "local"},
                    # TLS ends at the Edge. Keycloak builds its URLs from KC_HOSTNAME, which is AUTH_ISSUER's origin.
                    {"name": "KC_HTTP_ENABLED", "value": "true"},
                    {"name": "KC_HOSTNAME", "value": origin(public_url, "public URL")},
                    {"name": "KC_PROXY_HEADERS", "value": "xforwarded"},
                    {"name": "KC_HEALTH_ENABLED", "value": "true"},
                    *secret_env,
                ],
                "ports": [{"name": "http", "containerPort": 8080},
                          {"name": "management", "containerPort": 9000}],
                "startupProbe": {"httpGet": {"path": "/health/started", "port": "management"},
                                 "periodSeconds": 5, "failureThreshold": 60},
                "readinessProbe": {"httpGet": {"path": "/health/ready", "port": "management"},
                                   "periodSeconds": 10},
                # Keycloak rebuilds its server image under /opt/keycloak/lib at start when the build options
                # differ from the image's, so the root filesystem stays writable. Privileges are still dropped.
                "securityContext": {"allowPrivilegeEscalation": False,
                                    "readOnlyRootFilesystem": False,
                                    "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "200m", "memory": "512Mi"},
                              "limits": {"cpu": "1", "memory": "1Gi"}},
                "volumeMounts": [{"name": "data", "mountPath": "/opt/keycloak/data/h2"},
                                 {"name": "realm", "mountPath": "/opt/keycloak/data/import", "readOnly": True},
                                 {"name": "tmp", "mountPath": "/tmp"}],
            }],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "target-login"}},
                        {"name": "realm", "configMap": {"name": "target-login-realm"}},
                        {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}}],
        }},
    })
    # Browsers reach the login pages through the Edge tunnel; nothing else needs Keycloak. demo-chat fetches
    # the issuer's public keys through the same public hostname. Keycloak itself calls nothing.
    policy = resource("networking.k8s.io/v1", "NetworkPolicy", "target-login-edge-only", spec={
        "podSelector": {"matchLabels": SELECTOR}, "policyTypes": ["Ingress", "Egress"],
        "ingress": [{"from": [{"podSelector": {"matchLabels": EDGE}}],
                     "ports": [{"protocol": "TCP", "port": 8080}]}],
        "egress": [],
    })
    return [volume, claim, config, service, deploy, policy]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-name", required=True)
    parser.add_argument("--public-url", required=True, help="https origin the Edge serves the login pages on")
    parser.add_argument("--app-origin", required=True, help="https origin of demo-chat (its sign-in callback is /)")
    args = parser.parse_args()
    items = identity(args.node_name, args.public_url, args.app_origin)
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=2))


if __name__ == "__main__":
    main()
