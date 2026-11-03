# T12 platform state DB network path

`render.py` creates two additional policies. `platform-state` accepts TCP 5432
from `bravo-platform` Pods with the exact
`app.kubernetes.io/name=cutover-executor` label. Those caller Pods get egress to
that DB selector and cluster DNS only through this policy. Other reviewed
control routes require their own policies.

Both namespace and Pod selectors occur in the same peer, so matching a label
in another namespace is insufficient. Existing default-deny, T10 runtime,
CNPG operator and Collector policies are not modified. NetworkPolicy selects
Pod labels, not ServiceAccount identities; the dedicated ServiceAccount,
SQL credentials, schema grants and TLS verification remain T12's separate
contracts.

The endpoint is `platform-db-rw.platform-state.svc:5432`, database `platform`,
with `sslmode=verify-full`. This platform DB has no PgBouncer in front of it.
The T10 runtime credential must not be reused for the new Executor.

Before applying, verify current cluster/namespace, Service and ready endpoints;
reject existing objects with these new policy names. Dry-run the exact payload,
create only these policies and compare all previous policy specifications
before/after. A successful apply/readback is not evidence of successful SQL
traffic or the Collector's actual denial; verify those from the real callers.

## Executor control routes over Tailscale

`control_network.py` renders new objects for the Executor's remote DB and
PgBouncer connections. It does not modify existing app Services, install an
operator, register nodes, change tailnet ACLs or enable a Kubernetes API proxy.
It uses the same chart 1.104.4 and exact operator/proxy image digests as the T8
network preparation. The chart hash is `CHART_SHA256` in the renderer; verify
the downloaded archive against it before rendering or installation.

| Stage | Output |
| --- | --- |
| `values` | Platform operator values referencing the existing `tailscale-system/operator-oauth` Secret; credential values are empty. API proxy and impersonation are disabled. |
| `pooler-ingress --side source` / `--side target` | New `side-control-pooler-tailnet` Service, TCP 6432 to existing `app=side-pgbouncer` TCP 5432. |
| `routes` | One `bravo-cutover-control` egress ProxyGroup and four ExternalName Services in `bravo-platform`. |
| `network` | Executor egress NetworkPolicy derived from current generated Service port mappings supplied as JSON on stdin. |

The platform operator has `tag:bravo-platform-operator`; the ProxyGroup has
`tag:bravo-cutover-control` and one replica. This is a development configuration,
not a high-availability claim. The source/target pooler routes have separate
`tag:bravo-source-control-pooler` and `tag:bravo-target-control-pooler` tags.
Tailnet port grants and namespace ingress policies must be reviewed separately.
The new Service alone does not establish that either layer permits traffic.

The four default routes are `source-db-for-cutover` and `target-db-for-cutover`
on TCP 5432, plus `source-pooler-for-cutover` and `target-pooler-for-cutover` on
TCP 6432. Supply independently verified FQDNs using `--source-db-fqdn`,
`--target-db-fqdn`, `--source-pooler-fqdn`, and `--target-pooler-fqdn`. They must
have the exact names `bravo-side-db.<tailnet>.ts.net` and
`bravo-side-control-pooler.<tailnet>.ts.net`, all in the same tailnet. This syntax
check does not prove account or node ownership; the operator must verify both
before rendering.

API routes are omitted by default. After separate HTTPS, identity and RBAC
verification, `routes --include-api` additionally requires
`--source-api-fqdn bravo-source-operator.<tailnet>.ts.net` and
`--target-api-fqdn bravo-target-operator.<tailnet>.ts.net`. It adds the two
`side-api-for-cutover` Services on TCP 443. This option only renders a route;
it does not enable an API proxy or grant Kubernetes authorization. Consumers
must retain TLS verification against the real certificate hostname. An
ExternalName alias by itself does not change a server certificate's identity.

### Bind the NetworkPolicy to actual proxy ports

Tailscale 1.104.4 creates a separate ClusterIP Service and maps each remote port
to an allocated proxy Pod port in `[3000, 4000)`. Thus a Pod policy allowing
only 5432/6432 can block actual post-DNAT traffic. The behavior is specified in
the pinned [egress service controller](https://github.com/tailscale/tailscale/blob/v1.104.4/cmd/k8s-operator/egress-services.go#L84-L101).

After the operator has created the selected routes, read its Service objects
from `tailscale-system` as JSON and pass the fresh List to `network`:

```sh
python3 infra/envs/executor-network/control_network.py network < generated-services.json > control-policy.json
```

Keep actual FQDNs, raw Service readbacks and generated operational manifests
in the private collaboration area. They can contain resource UIDs and network
addresses and are not public evidence without the repository's redaction check.

Use `--include-api` on both `routes` and `network` only when the API paths have
been verified. The renderer requires all four (or all six) mappings and checks
their UIDs, namespaces, parent Service labels, ProxyGroup identity and remote
ports. It rejects missing/duplicate routes, extra routes from this group, named
or out-of-range target ports, and unexpected business ports. Unrelated Services
outside this ProxyGroup are ignored. It permits the selected Service ports and
only their observed translated ports, never the whole allocation range or
the proxy health port 9002.

The policy selects only `bravo-platform` Pods labelled
`app.kubernetes.io/name=cutover-executor`. Its destination peer combines the
`tailscale-system` namespace and the exact three proxy labels
`tailscale.com/managed=true`, `tailscale.com/parent-resource-type=proxygroup`,
and `tailscale.com/parent-resource=bravo-cutover-control`. These labels come from
the pinned controller's [pgLabels function](https://github.com/tailscale/tailscale/blob/v1.104.4/cmd/k8s-operator/proxygroup_specs.go#L613-L621),
with key constants in [sts.go](https://github.com/tailscale/tailscale/blob/v1.104.4/cmd/k8s-operator/sts.go#L49-L51).
The state DB policy supplies the caller's DNS egress. This additional policy
does not isolate proxy ingress or grant proxy internet access; preserve and
verify the operator's existing networking separately.

Generated Service ports can change when resources are recreated. Bind the
readback to the intended cluster and the exact current Service/ProxyGroup UIDs,
review the rendered policy and use server-side dry runs before creating it.
Re-read the Service mappings after changes and stop integration if they differ
from the evidence. Do not replace the operator's generated Services to force
stable ports. Verify real positive and negative traffic from the intended
caller; rendering, readiness and a policy readback are not a connection test.
