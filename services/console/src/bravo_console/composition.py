"""Host composition over existing T10 services and registered owner artifacts.

All parameters are deployment-owned. There is no HTTP configuration endpoint.
The same verified session resolver is reused for T10 and receipt ownership.
"""

from starlette.requests import Request

from .app import ConsoleApp
from .audit import t24_reader
from .operation_reads import T10Operations
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
    load_audit=None,
    authenticate_audit=None,
    observations=None,
    controls=None,
    security=None,
):
    """`api` must be the native T10 app using these same service instances/store.

    Authorizers must check current host grants, not just tenant membership.
    Account costs and environment-wide metrics require their own grants.
    Initial dispatch additionally requires a positive check under the T10 lock.
    Omitted providers stay explicitly UNCONFIGURED and do not grant actions.
    """
    sources = {}
    for feature, loader in (registered_loaders or {}).items():
        if feature not in set(FEATURES) - {"audit", "monitoring"}:
            raise ValueError("registered_feature_not_supported")
        sources[feature] = RegisteredPanelSource(
            feature, load=loader, authenticate=authenticate_registration
        )
    if metrics_client is not None:
        sources["monitoring"] = T37Details(metrics_client, environments=environments)
    if load_audit is not None:
        sources["audit"] = AuditPanel(
            t24_reader(api, load=load_audit, authenticate=authenticate_audit)
        )
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
    return ConsoleApp(
        api,
        public_origin=public_origin,
        observations=observations,
        controls=controls,
        security=security,
        workspace=workspace,
    )
