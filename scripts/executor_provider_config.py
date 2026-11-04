"""Render a reviewed provider-config addition; never write host files or restart RKE2."""
import copy
import json
import re
import sys


def extend(current, account):
    if not re.fullmatch(r"[0-9]{12}", account):
        raise ValueError("expected_account_invalid")
    registry = f"{account}.dkr.ecr.ap-northeast-2.amazonaws.com/bravo/"
    previous, executor = registry + "demo-chat", registry + "cutover-executor"
    if (not isinstance(current, dict) or set(current) != {"apiVersion", "kind", "providers"} or
        current.get("apiVersion") != "kubelet.config.k8s.io/v1" or current.get("kind") != "CredentialProviderConfig"):
        raise ValueError("provider_config_type_mismatch")
    providers = current.get("providers")
    if not isinstance(providers, list) or len(providers) != 1:
        raise ValueError("provider_inventory_mismatch")
    provider = providers[0]
    if (not isinstance(provider, dict) or provider.get("name") != "ecr-credential-provider" or
        provider.get("apiVersion") != "credentialprovider.kubelet.k8s.io/v1" or
        provider.get("defaultCacheDuration") != "0s" or
        provider.get("env") != [{"name": "AWS_REGION", "value": "ap-northeast-2"}] or
        set(provider) != {"name", "apiVersion", "defaultCacheDuration", "env", "matchImages"} or
        provider.get("matchImages") not in ([previous], [previous, executor])):
        raise ValueError("provider_contract_mismatch")
    desired = copy.deepcopy(current)
    desired["providers"][0]["matchImages"] = [previous, executor]
    return desired


if __name__ == "__main__":
    try:
        data = json.load(sys.stdin)
        print(json.dumps(extend(data["current"], data["expected_account"]), indent=2))
    except (KeyError, TypeError, ValueError):
        print("Provider configuration did not match the reviewed boundary.", file=sys.stderr)
        raise SystemExit(1) from None
