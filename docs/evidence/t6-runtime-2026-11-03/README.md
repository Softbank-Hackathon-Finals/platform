# T6 EKS foundation — 2026-11-03

Actual AWS validation used the existing individual IAM operator in the reviewed T1 account. It did not use SSO.

| Check | Result |
|---|---|
| Terraform fmt/init/validate | PASS |
| Mock boundary tests | 3 PASS |
| Trivy HIGH/CRITICAL | 0 findings in this stack |
| EKS/nodegroup | ACTIVE; Kubernetes 1.35 |
| Kubernetes API | private endpoint; CA/TLS server name checked through SSM tunnel; `/readyz=ok` |
| Nodes | 1/1 Ready |
| Namespace ConfigMap read | actual request allowed |
| Secret read and other-namespace request | actual requests returned Forbidden |
| EKS audit canary | ConfigMap create event (201) observed in CloudWatch |
| Disposable test namespace | removed |
| Individual SSO | NOT VERIFIED |
| App/DB/CNPG/PgBouncer/ESO/cutover | separate workload follow-up |

[Machine-readable results](results.json) include the measured scope and hashes of the runtime source files. Raw provider responses, expected account, object IDs, command IDs, endpoint/CA and original canary namespace are preserved in the operator's private evidence directory. They are not published here. The workload verification session reuses this cluster and owns dependent workload resources separately. T6 remains in progress.
