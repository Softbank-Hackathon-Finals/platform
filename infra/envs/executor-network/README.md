# T12 platform state DB network path

> **2026-11-07 · T44:** 이 렌더러의 경로 출력(`routes`·`network`·`proxy-ingress`)은 [`infra/iac`](../../iac/README.md)의 `tailnet-egress` 모듈로 옮겼다. 같은 ProxyGroup·Service·NetworkPolicy의 주인은 하나다. CLI의 해당 명령은 종료 코드 2로 거부한다. `pooler-ingress`(출발지·목적지 제어 Pooler 장치)도 `tailnet-ingress` 모듈로 옮겼다. 남은 것: `values`.

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
operator, register nodes or change tailnet ACLs. The operator values enable the
platform's Kubernetes API proxy in auth mode only (see `values` below).
It uses the same chart 1.104.4 and exact operator/proxy image digests as the T8
network preparation. The chart hash is `CHART_SHA256` in the renderer; verify
the downloaded archive against it before rendering or installation.

| Stage | Output |
| --- | --- |
| `values` | Platform operator values referencing the existing `tailscale-system/operator-oauth` Secret; credential values are empty. The API proxy runs in auth mode (`apiServerProxyConfig.mode: "true"`, never `noauth`): it impersonates the Kubernetes group a tailnet grant names, and the chart adds the `tailscale-auth-proxy` impersonation ClusterRole. Who may use it and with which role is declared in `infra/iac` (`operator-access`, T44). |
| `pooler-ingress --side source` / `--side target` | New `side-control-pooler-tailnet` Service, TCP 6432 to existing `app=side-pgbouncer` TCP 5432. |
| `routes` | One `bravo-cutover-control` egress ProxyGroup and four ExternalName Services in `bravo-platform`. |
| `network` | Executor egress NetworkPolicy derived from current generated Service port mappings supplied as JSON on stdin. |
| `proxy-ingress --transport-port <observed-port>` | Ingress NetworkPolicy for only the control ProxyGroup; uses the same Service JSON and one or two verified UDP listen ports. Repeat `--transport-port` when IPv4 and IPv6 use different ports. |

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
to an allocated proxy Pod port in `[10000, 11000)`. Thus a Pod policy allowing
only 5432/6432 can block actual post-DNAT traffic. The behavior is specified in
the pinned [allocator implementation](https://github.com/tailscale/tailscale/blob/27047635f237d1f3e0b08c0f487674757f30d2f7/cmd/k8s-operator/egress-services.go#L644-L655):
`unusedPort()` adds 10000 to a random integer below `maxPorts`, whose value is
[1000](https://github.com/tailscale/tailscale/blob/27047635f237d1f3e0b08c0f487674757f30d2f7/cmd/k8s-operator/egress-services.go#L55-L61).
An earlier descriptive comment in that same source file still mentions an old
3000-based range. The renderer follows the executable allocator and rejects
unknown or out-of-range mappings. The allocation range validates readback;
it is never emitted as a range permission.

After the operator has created the selected routes, read its Service objects
from `tailscale-system` as JSON and pass the fresh List to `network`:

```sh
python3 infra/envs/executor-network/control_network.py network < generated-services.json > control-policy.json
```

Keep actual FQDNs, raw Service readbacks and generated operational manifests
in the private collaboration area. They can contain resource UIDs and network
addresses and are not public evidence without the repository's redaction check.

Use `--include-api` consistently on `routes`, `network` and `proxy-ingress` only when the API paths have
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
the pinned controller's [pgLabels function](https://github.com/tailscale/tailscale/blob/27047635f237d1f3e0b08c0f487674757f30d2f7/cmd/k8s-operator/proxygroup_specs.go#L613-L621),
with key constants in [sts.go](https://github.com/tailscale/tailscale/blob/27047635f237d1f3e0b08c0f487674757f30d2f7/cmd/k8s-operator/sts.go#L49-L51).
The state DB policy supplies the caller's DNS egress. The `network` stage retains
its single caller egress policy. It does not grant proxy internet access;
preserve and verify the operator's existing egress separately.

### Restrict the control proxy's callers

The separate `proxy-ingress` stage selects only the exact three control
ProxyGroup labels above in `tailscale-system`. It creates three ingress rules:

- The `bravo-platform` namespace **and** the exact Executor label may reach
  only the observed TCP business ports and their selected Service ports.
- The `tailscale-system` namespace **and** `app=operator` may reach TCP 9002
  for operator health checks. This rule grants no business connection.
- Any source may reach only the explicitly supplied UDP listen ports for
  authenticated WireGuard transport. This rule grants no cleartext TCP access.

This policy does not select other ProxyGroups or replication proxies and does
not change any egress rule. Kubernetes NetworkPolicies are additive: another
policy selecting these same Pods could permit additional traffic. Inspect all
such policies and test rejection from Pods with a wrong namespace or label
before treating the Executor boundary as enforced, especially before enabling
an API route. Labels are not ServiceAccount identities; namespace workload
permissions and the downstream API's authentication/RBAC still need validation.

Do not assume UDP 41641. The pinned Linux daemon's
[defaultPort implementation](https://github.com/tailscale/tailscale/blob/27047635f237d1f3e0b08c0f487674757f30d2f7/cmd/tailscaled/tailscaled.go#L107-L121)
returns zero when `PORT` is unset, allowing the kernel to allocate a port.
IPv4 and IPv6 can use different ports. The proxy generator
[sets `PORT` only when supplied a static endpoint port](https://github.com/tailscale/tailscale/blob/27047635f237d1f3e0b08c0f487674757f30d2f7/cmd/k8s-operator/proxygroup_specs.go#L196-L200).
For this single-replica development deployment, read the current tailscaled
process's UDP sockets, correlate their socket inodes with `/proc/net/udp` and
`/proc/net/udp6`, and bind the evidence to the current Pod UID and cluster.
The runtime operator must verify those observations before supplying them:
the renderer validates the input shape, not the truth or freshness of the
observation. It requires a list of one or two distinct integers in 1–65535 and
rejects collisions with the selected business ports or reserved 443/5432/6432/9002.

```sh
python3 infra/envs/executor-network/control_network.py proxy-ingress \
  --transport-port "$OBSERVED_IPV4_UDP_PORT" \
  --transport-port "$OBSERVED_IPV6_UDP_PORT" \
  < generated-services.json > control-proxy-ingress-policy.json
```

Supply one argument if only one port was observed or both families use the
same port. The Python interface is
`proxy_ingress(service_list, include_api=False, transport_ports=[...])`; the
keyword argument is required. After any Pod replacement, recheck its UID and
UDP sockets and update this policy before relying on direct transport. Stale
ports can block the direct path even if relay connectivity hides the problem.
This renderer does not pin the daemon's port or add a ProxyClass rollout;
automated refresh or an explicitly configured stable port is follow-up work
before persistent or multi-replica operation.

Generated Service ports can change when resources are recreated. Bind the
readback to the intended cluster and the exact current Service/ProxyGroup UIDs,
review the rendered policy and use server-side dry runs before creating it.
Re-read the Service mappings after changes and stop integration if they differ
from the evidence. Do not replace the operator's generated Services to force
stable ports. Verify real positive and negative traffic from the intended
caller; rendering, readiness and a policy readback are not a connection test.

## Source PgBouncer backend route to the target DB

`pooler_backend.py` prepares a separate source pooler route using the same
ProxyGroup and ExternalName Service contract as T8's `target-workloads/network.py`
egress renderer. Run it for the source cluster, whose existing pinned Tailscale
1.104.4 operator lives in `tailscale-system`:

```sh
python3 infra/envs/executor-network/pooler_backend.py \
  --target-db-fqdn "$VERIFIED_TARGET_DB_FQDN" > source-pooler-backend.json
```

Keep this generated manifest in the private collaboration area. The command
outputs exactly two new objects:

- A cluster-scoped `bravo-source-pooler` ProxyGroup, type `egress`, one replica,
  with only `tag:bravo-source-pooler`. Its proxy resources belong to the source
  operator's namespace; the ProxyGroup itself has no namespace.
- `source-dev/target-db-for-pooler`, an ExternalName Service with the initial
  `externalName: placeholder`, `tailscale.com/proxy-group: bravo-source-pooler`,
  the verified target DB FQDN annotation, and only TCP 5432. The operator
  reconciles this placeholder into its generated Service path.

The FQDN must be exactly `bravo-target-db.<tailnet>.ts.net`. Syntax validation
does not establish the selected account, tailnet or destination ownership;
verify those against current source and target observations first. The runtime
owner manages `tag:bravo-source-pooler` ownership by
`tag:bravo-source-operator` and a grant only to `tag:bravo-target-db` on TCP 5432.
The renderer creates no ACL or operator configuration.

The intended backend address is
`target-db-for-pooler.source-dev.svc:5432`. This preparation does not switch
PgBouncer to that backend, alter either pooler Deployment or `hostAliases`,
or reuse/change a replication identity. The default `routes` stage creates
no NetworkPolicy. Verify the source pooler's egress to this exact proxy and
the target DB's ingress separately. A dedicated tailnet tag separates the
route's identity but does not itself restrict which in-cluster Pods can use
its Service. Retain the
backend TLS verification contract: the Service alias does not rewrite the
database certificate's hostname.

After the new proxy is running, the separate `proxy-ingress` stage uses the
same child Service ownership, UID and allocated-port validation as the
Executor renderer. Supply a fresh Service List from the source operator's
namespace and the proxy's current Pod-bound UDP socket observations:

```sh
python3 infra/envs/executor-network/pooler_backend.py proxy-ingress \
  --transport-port "$OBSERVED_IPV4_UDP_PORT" \
  --transport-port "$OBSERVED_IPV6_UDP_PORT" \
  < source-generated-services.json > source-pooler-ingress-policy.json
```

This policy selects only `bravo-source-pooler` proxy Pods in `tailscale-system`.
It admits TCP 5432 and the one observed translated port only from
`source-dev` **and** `app=source-pgbouncer`; only `tailscale-system` **and**
`app=operator` may use TCP 9002. The separate UDP rule admits only the one or
two supplied transport ports. The input rejects duplicate/missing/foreign
route mappings, invalid UIDs or ownership labels, unexpected business ports,
and transport collisions. Other ProxyGroups' Services are ignored.
The Python entry point is
`proxy_ingress(service_list, transport_ports=[...])` with the keyword required.
The same additive-policy and UDP-port freshness limitations described above
apply here. Coordinate the source pooler's health probes and actual traffic
checks before applying a policy that isolates its new proxy; this stage does
not change the application's probes or its own egress policy.

Before use, verify the source cluster context and the absence of conflicting
objects, review and dry-run the manifest, then read back the new ProxyGroup,
its child Service mappings and ready endpoints. Runtime validation must use
the actual source pooler and include forbidden callers; successful rendering
or readiness alone does not prove a DB connection or successful cutover.
The one-replica route is for the current development environment.

On source EKS, first detect whether the VPC CNI actually enforces NetworkPolicy:
verify `enableNetworkPolicy: "true"` in the live add-on configuration,
`--enable-network-policy=true` on the nodeagent, and generated PolicyEndpoints
covering this exact proxy Pod and the intended peer addresses/ports. A healthy
agent or a stored NetworkPolicy alone does not establish enforcement. The
[source EKS configuration](../source-eks/README.md#network-policy-enforcement)
sets this feature explicitly and documents verification of the dedicated CNI
role on both the service account and newly created CNI Pods. Require those
Pods to become Ready with the expected web identity before traffic testing.
After it reconciles, require fresh successful
TLS connections from the actual source PgBouncer and failed business-port
connections from wrong-label and wrong-namespace callers. Successful isolation
on the separate RKE2 platform does not prove source EKS isolation.
