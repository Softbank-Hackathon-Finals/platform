# Capacity and existing-host resize

The single platform server hosts the RKE2 control plane, CNPG operator and state DB, Tailscale operator and control proxy, and the Executor. A two-vCPU server can boot RKE2 but cannot schedule this resident set. The default is now `t3.xlarge`, with the selected type's full CPU options. A plan rejects types below four vCPUs / 8 GiB or without x86_64 support. This floor follows the observed workload and [RKE2's recommended server minimum](https://docs.rke2.io/install/requirements); it does not prove capacity for future workloads.

## Measured scheduling boundary

Read-only inventory on 2026-11-04 KST found one Ready node with 2,000m allocatable CPU and 1,976m requested by active scheduled Pods:

| Resident workload | Effective CPU requests |
|---|---:|
| RKE2 system components | 1,575m |
| CNPG operator | 100m |
| Platform state DB | 200m |
| Tailscale operator | 100m |
| Control proxy | 1m |
| **Total / remaining** | **1,976m / 24m** |

The total excludes terminal and unscheduled Pods and accounts for init containers. The node's instantaneous usage was only 214m; the scheduler admits workloads by requests, so low current usage does not remove the shortage. No unused temporary Pod was available to reclaim. The 100m Executor requires at least 2,076m with the existing residents, and two concurrent 100m control probes raise that to 2,276m. A sequential 10m read-only probe can verify connectivity but cannot demonstrate Executor scheduling or load capacity. Do not lower the service's requests based on that probe or a single usage sample.

## Existing-host change boundary

Changing an EBS-backed EC2 instance type requires a stop/start. This is an interruption to the platform DB and control path, even when Terraform reports an in-place update. [AWS resize guidance](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/change-instance-type-of-ebs-backed-instance.html) also warns that existing CPU options can be retained. The configuration explicitly requests the selected type's default cores and threads. AWS provider 6.71.0 applies instance-type and CPU-option changes separately and can perform more than one stop/start; treat the whole apply as one coordinated outage window, not a zero-downtime update.

The measured DB has a 16 GiB local PV with `Retain`, mounted from a separate encrypted 16 GiB EBS volume, with EC2 `DeleteOnTermination=false`. The 40 GiB root volume is separate. These retention settings preserve storage on deletion but do not prove a recoverable database or preserve cluster state. The inventory found one local etcd snapshot, no CNPG Backup objects and no CNPG backup configuration. This is **not an off-host recovery guarantee**. `prevent_destroy` blocks resource replacement while the host resource remains in this configuration. It does not replace backups or protect against removing the resource block.

Before applying a resize:

1. Obtain the infrastructure code-owner review and agree the additional cost, retention duration and interruption window. The runtime, DB/Executor and control-network owners must confirm their active probes have finished and temporary credentials/Jobs have been removed.
2. Use the actual host's existing backend and private inputs; verify account, VPC, instance ID, AMI, private IP, ENI, root and DB volume IDs/encryption/attachment/delete-on-termination settings and CPU options. Preserve Terraform state lineage. Confirm the instance is EBS-backed On-Demand and is not managed by an Auto Scaling group.
3. Reconcile all configuration already applied to that state. At this audit, [PR #92](https://github.com/Softbank-Hackathon-Finals/platform/pull/92) owns the live private ECR read policy but is not yet merged. Planning develop without it proposes deleting the policy. Merge the dependency under normal review rules and make a **fresh complete plan**. Do not use `-target`, ignore drift, import a duplicate host or apply a temporary combined source snapshot.
4. With the DB owner, make current encrypted off-host DB and etcd recovery material, record restore prerequisites and perform an isolated recovery check. Bind the backup to the current state DB and host and both volumes. The earlier local snapshot alone is insufficient; do not print or copy credentials into review artifacts.
5. Save the full plan and require exactly one managed resource update, `aws_instance.node`, limited to `instance_type` and `cpu_options`: zero create/delete/replace, unchanged user data, AMI, networking, IAM and storage. A replacement or unrelated change stops this procedure. Do not disable `prevent_destroy` to make a resize pass.
6. Apply only that reviewed, fresh plan during the agreed window. Preserve the existing host and both volumes. No additional worker, request reduction, namespace deletion or workload recreation is part of this change.

## Acceptance and rollback

After the instance returns, verify the original instance/ENI/private IP/root and DB volume identities, four visible vCPUs and adequate Kubernetes allocatable CPU, Ready node and original bound PVC/PV. Verify the platform DB's health and the owner's state/schema/data invariants, mounted credentials, and the existing demo image read path. A failed readiness or data check keeps the deployment closed.

The control-network owner must reread the actual `tailscaled` UDP socket even if the Pod UID is unchanged; a container restart can choose another port. Recheck the proxy Pod and all four generated Service UIDs/ports, and update the exact network policy through the owner's compare-and-set process if needed. Then repeat the actual control-path/TLS/negative-credential checks and state DB checks. Finally, the T12 owner must show that the unmodified 100m Executor schedules and becomes healthy with the reviewed image. Report API/cutover and migration receipts separately; this resize does not establish them.

If the larger instance cannot start, preserve both volumes and failed-start evidence. Reverting to the previous type requires another reviewed stop/start and matching CPU options; it restores the original capacity limitation. Do not terminate/recreate the server, clear state or lower service requests as a rollback shortcut. Data recovery follows the separately verified backup procedure if the original volume is unusable.

## Price and alternatives

AWS Price List API, Seoul Linux shared-tenancy On-Demand, queried 2026-11-04 KST:

| Type | vCPU / memory | USD/hour | Difference from current node |
|---|---|---:|---:|
| t3.large (current) | 2 / 8 GiB | 0.1040 | — |
| t3.xlarge (same-family proposal) | 4 / 16 GiB | 0.2080 | +0.1040/hour |
| t3a.xlarge | 4 / 16 GiB | 0.1872 | +0.0832/hour |
| c6i.xlarge | 4 / 8 GiB | 0.1920 | +0.0880/hour |

The same-family proposal keeps the current CPU-family and burst-credit model while adding capacity. The cheaper alternatives need a separate choice and compatibility review; this change does not silently select them. With standard CPU credits, sustained performance still requires measurement. The proposed increment is US$2.496/day or US$75.92 per 730 hours, excluding existing EBS/NAT and other charges. The earlier T6+T7 baseline becomes US$394.20/month, above its recorded US$372.32 budget before extras. Code merge does not authorize an indefinite retention period or raise the account budget.
