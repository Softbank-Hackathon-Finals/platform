"""Allowlisted Kubernetes configuration facts, never a control or attack verdict."""

BOOL_CHECKS = {
    "host_network",
    "host_pid",
    "host_ipc",
    "token_automount",
    "privileged",
    "privilege_escalation",
    "non_root",
    "read_only_root",
    "drop_all_capabilities",
}
POLICY_CHECKS = {"ingress_policy_count", "egress_policy_count"}
CHECKS = BOOL_CHECKS | POLICY_CHECKS | {"seccomp"}


def _boolean(obj, key, *, default=None):
    value = obj.get(key, default)
    if value is not None and type(value) is not bool:
        raise ValueError("invalid_security_configuration")
    return value


def _matches(selector, labels):
    """Kubernetes LabelSelector, including the absent-label NotIn semantics."""
    if type(selector) is not dict or set(selector) - {"matchLabels", "matchExpressions"}:
        raise ValueError("invalid_policy_selector")
    match = selector.get("matchLabels", {})
    expressions = selector.get("matchExpressions", [])
    if (
        type(match) is not dict
        or type(expressions) is not list
        or len(match) + len(expressions) > 64
    ):
        raise ValueError("invalid_policy_selector")
    if any(type(k) is not str or type(v) is not str for k, v in match.items()):
        raise ValueError("invalid_policy_selector")
    selected = all(labels.get(k) == v for k, v in match.items())
    for entry in expressions:
        if type(entry) is not dict or set(entry) - {"key", "operator", "values"}:
            raise ValueError("invalid_policy_selector")
        key, op, values = entry.get("key"), entry.get("operator"), entry.get("values", [])
        if (
            type(key) is not str
            or not key
            or type(values) is not list
            or len(values) > 64
            or any(type(v) is not str for v in values)
        ):
            raise ValueError("invalid_policy_selector")
        if op in {"In", "NotIn"} and values:
            answer = labels.get(key) in values if op == "In" else labels.get(key) not in values
        elif op in {"Exists", "DoesNotExist"} and not values:
            answer = key in labels if op == "Exists" else key not in labels
        else:
            raise ValueError("invalid_policy_selector")
        selected = selected and answer
    return selected


def pod_checks(pod, policies):
    spec, metadata = pod["spec"], pod["metadata"]
    counts = {"Ingress": 0, "Egress": 0}
    for policy in policies:
        meta, rule = policy.get("metadata", {}), policy.get("spec", {})
        if meta.get("namespace") != metadata["namespace"] or not meta.get("uid"):
            raise ValueError("policy_identity_unconfirmed")
        if meta.get("deletionTimestamp"):
            continue
        if not _matches(rule.get("podSelector", {}), metadata.get("labels", {})):
            continue
        types = rule.get("policyTypes")
        if types is None:
            types = ["Ingress"] + (["Egress"] if rule.get("egress") else [])
        if (
            type(types) is not list
            or not types
            or len(set(types)) != len(types)
            or any(t not in counts for t in types)
        ):
            raise ValueError("invalid_policy_types")
        for kind in types:
            counts[kind] += 1
    return [
        {"id": "host_network", "value": _boolean(spec, "hostNetwork", default=False)},
        {"id": "host_pid", "value": _boolean(spec, "hostPID", default=False)},
        {"id": "host_ipc", "value": _boolean(spec, "hostIPC", default=False)},
        # A missing Pod setting can inherit from its ServiceAccount, which is
        # deliberately not read. Never infer false/true for that case.
        {"id": "token_automount", "value": _boolean(spec, "automountServiceAccountToken")},
        {"id": "ingress_policy_count", "value": counts["Ingress"]},
        {"id": "egress_policy_count", "value": counts["Egress"]},
    ]


def container_checks(pod, container):
    pod_context = pod["spec"].get("securityContext") or {}
    context = container.get("securityContext") or {}
    seccomp = context.get("seccompProfile", pod_context.get("seccompProfile"))
    seccomp_type = seccomp.get("type") if isinstance(seccomp, dict) else None
    if seccomp_type not in {None, "Unconfined", "RuntimeDefault", "Localhost"}:
        raise ValueError("invalid_security_configuration")
    drops = context.get("capabilities", {}).get("drop")
    if drops is not None and (type(drops) is not list or any(type(x) is not str for x in drops)):
        raise ValueError("invalid_security_configuration")
    return [
        {"id": "privileged", "value": _boolean(context, "privileged")},
        {"id": "privilege_escalation", "value": _boolean(context, "allowPrivilegeEscalation")},
        {
            "id": "non_root",
            "value": _boolean(context, "runAsNonRoot", default=pod_context.get("runAsNonRoot")),
        },
        {"id": "read_only_root", "value": _boolean(context, "readOnlyRootFilesystem")},
        {"id": "drop_all_capabilities", "value": None if drops is None else "ALL" in drops},
        {"id": "seccomp", "value": seccomp_type},
    ]


def validate_configuration(value, nodes):
    if type(value) is not dict or len(value) > 23:
        raise ValueError("invalid_security_configuration")
    by_id = {n["id"]: n for n in nodes}
    for resource, item in value.items():
        if (
            resource not in by_id
            or type(item) is not dict
            or set(item) != {"resource_uid", "checks"}
            or item["resource_uid"] != by_id[resource]["resource_uid"]
            or type(item["checks"]) is not list
            or not 1 <= len(item["checks"]) <= 12
        ):
            raise ValueError("security_configuration_scope_mismatch")
        seen = set()
        for check in item["checks"]:
            if type(check) is not dict or set(check) != {"id", "value"}:
                raise ValueError("invalid_security_configuration")
            key, fact = check["id"], check["value"]
            if key not in CHECKS or key in seen:
                raise ValueError("invalid_security_configuration")
            seen.add(key)
            if key in BOOL_CHECKS:
                valid = fact is None or type(fact) is bool
            elif key in POLICY_CHECKS:
                valid = type(fact) is int and 0 <= fact <= 256
            else:
                valid = fact is None or fact in {"Unconfined", "RuntimeDefault", "Localhost"}
            if not valid:
                raise ValueError("invalid_security_configuration")
