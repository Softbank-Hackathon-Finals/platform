"""Host composition over existing T10 services and registered owner artifacts.

All parameters are deployment-owned. There is no HTTP configuration endpoint.
The same verified session resolver is reused for T10 and receipt ownership.
"""

from starlette.requests import Request

from .app import ConsoleApp
from .audit import PlanAudit, t24_reader
from .delivery import PlanDelivery
from .operation_reads import T10Operations
from .workload import PlanWorkload
from .workspace_data import AuditPanel, PlanWorkspace, RegisteredPanelSource, T37Details
from .workspace_reports import FEATURES


def compose_console(
    api,
    store,
    *,
    public_origin,
    resolve_principal,
    authorize_panel,
    authorize_operation,
    registered_loaders=None,
    authenticate_registration=None,
    shift=None,
    failback=None,
    rehearsal=None,
    rehearsal_tasks=None,
    initial=None,
    check_initial=None,
    metrics_client=None,
    environments=None,
    audit=None,
    load_audit=None,
    audit_client=None,
    audit_bucket=None,
    audit_root=None,
    authenticate_audit=None,
    observations=None,
    controls=None,
    security=None,
    network_client=None,
    authorize_network=None,
    network_registration=None,
    workload=None,
    delivery=None,
):
    """`api` must be the native T10 app using these same service instances/store.

    Authorizers must check current host grants, not just tenant membership.
    Account costs and environment-wide metrics require their own grants.
    Initial dispatch additionally requires a positive check under the T10 lock.
    Omitted providers stay explicitly UNCONFIGURED and do not grant actions.
    """
    if workload is not None and (type(workload) is not PlanWorkload or workload.api is not api):
        raise ValueError("workload_provider_requires_same_t10_api")
    if delivery is not None and (type(delivery) is not PlanDelivery or delivery.api is not api):
        raise ValueError("delivery_provider_requires_same_t10_api")
    sources = {}
    for feature, loader in (registered_loaders or {}).items():
        if feature not in set(FEATURES) - {"audit", "monitoring"}:
            raise ValueError("registered_feature_not_supported")
        sources[feature] = RegisteredPanelSource(
            feature, load=loader, authenticate=authenticate_registration
        )
    if metrics_client is not None:
        sources["monitoring"] = T37Details(metrics_client, environments=environments)
    if audit is not None and load_audit is not None:
        raise ValueError("one_audit_provider_required")
    if load_audit is not None:
        audit = t24_reader(api, load=load_audit, authenticate=authenticate_audit,
                           client=audit_client, bucket=audit_bucket, root=audit_root)
    if audit is not None:
        if type(audit) is not PlanAudit or audit.api is not api:
            raise ValueError("audit_provider_requires_same_t10_api")
        sources["audit"] = AuditPanel(audit)
    operations = T10Operations(
        api,
        store,
        resolve_principal=lambda scope: resolve_principal(Request(scope)),
        authorize=authorize_operation,
        shift=shift,
        failback=failback,
        rehearsal=rehearsal,
        rehearsal_tasks=rehearsal_tasks,
        initial=initial,
        check_initial=check_initial,
    )
    workspace = PlanWorkspace(
        api, sources=sources, authorize=authorize_panel, operations=operations
    )
    network = None
    if network_client is not None:
        from .network import PlanNetworkAtlas

        network = PlanNetworkAtlas(
            api, network_client, authorize=authorize_network, registration=network_registration
        )
    return ConsoleApp(
        api,
        public_origin=public_origin,
        observations=observations,
        controls=controls,
        security=security,
        audit=audit,
        workspace=workspace,
        network=network,
        workload=workload,
        delivery=delivery,
    )
