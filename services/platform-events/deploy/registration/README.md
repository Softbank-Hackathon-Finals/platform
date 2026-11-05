# Registration host deployment

The registration host uses the canonical `registration_server`, the existing
schema 3 runtime DB credential, and the [registration contract](../../../../docs/contracts/T10-registration.md).
It is separate from the window intake and product read service. Completing their
deployment is not a prerequisite for environment registration.

Run `deploy/product-read/build.py` from the clean, committed deployment checkout
to build the locked Python dependencies and same-revision source base. Pass the
verified local image tag from that build receipt as `BASE_IMAGE` to this
directory's Dockerfile. The registration image adds Git for the read-only
approved checkout checks. Record the final image digest, source revision and
archive digest; verify them before import. An image build does not register a
tenant, workload or plan.

```sh
uv run --project services/platform-events --no-sync python services/platform-events/deploy/product-read/build.py --output /private/new-base-output
uv run --project services/platform-events --no-sync python services/platform-events/deploy/registration/build.py \
  --base-receipt /private/new-base-output/build.json --output /private/new-registration-output
```

The wrapper requires a clean checkout and matching source revision, local base
image ID and runtime user. It tests imports and Git without network access and
records the installed Git version and final archive hash.

Mount these inputs read-only for UID/GID 10001:

| Mount | Owner-controlled input |
|---|---|
| `/credential`, `/ca` | Existing runtime DB credential and database CA; no migration credential |
| `/registration/catalog.json` | Exact SSO verifier, actual environment references and accepted source release |
| `/registration/grants.json` | Current registration-only subject/tenant grants |
| Environment CA/token paths in the catalog | Dedicated collector credentials, separate from Executor and human admin credentials |
| Release checkout path in the catalog | Approved repository, commit and app/IaC paths, including its Git object database |
| `/tls/tls.crt`, `/tls/tls.key` | Dedicated registration host TLS identity |

Use one replica, a read-only root filesystem, dropped capabilities, no privilege
escalation and no automatic Kubernetes API token. Limit ingress to the approved
operator transport. Limit egress to the runtime DB, DNS, the selected regional
STS and the registered source/target APIs. Do not borrow rehearsal/apply proxy
labels to bypass another role's network boundary.

The TLS entrypoint listens on 8443 with request access logging disabled.
Supplying only a certificate or key is rejected before DB credentials are read.
Existing operator TLS ingress can retain the plain 8080 backend by invoking
the module without TLS arguments. Never expose that backend directly to callers.

Verify actual SSO authentication and wrong audience/expiry rejection, each
collector's self identity and read/write permissions, environment/workload/plan
registration and identical-key replay, then current DB rows and initial COLLECT
state. Publish TaskCatalog records only after the actual plan receipt exists.
T19 additionally requires both environments' authentic original control runs;
missing or UNKNOWN evidence must not be replaced with a synthetic PASS.


`render.py --inputs PRIVATE_INPUTS --catalog PRIVATE_CATALOG --grants PRIVATE_GRANTS
--output NEW_PRIVATE_MANIFEST` renders the registration ServiceAccount, immutable
ConfigMap, standard NetworkPolicy, Service and Deployment. It never contacts a
cluster, writes a Secret, or grants RBAC. Keep the catalog and rendered manifest
private because they contain actual environment and operator identity references.

The inputs select an immutable image digest, its `source_revision`, existing
`collector_secret` and `tls_secret` names, and `egress_ipv4`: observed IPv4 lists
for the source API, regional STS, and GitHub. The host aliases and exact `/32`
rules use the same observation. This is a bounded deployment window; re-resolve
and validate those inputs before renewal or replacement. It is not a DNS update
controller. Do not use a broad HTTPS exception when an address changes.

The init container receives only an empty checkout volume and fetches the
approved commit. It cannot mount the runtime DB, TLS key or collector tokens.
The registration process mounts that checkout read-only. Existing runtime DB
and dedicated target collector policies remain separate, additive permissions.
Inspect the actual CNI before choosing a policy type; this standard policy also
works on the observed Canal platform.
