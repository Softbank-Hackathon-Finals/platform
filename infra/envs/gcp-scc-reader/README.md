# T33 Google Cloud SCC 읽기 연합 (`gcp-scc-reader`)

플랫폼의 보안 탐지 수집기(`bravo_findings.gcp_pull`)가 GCP 프로젝트의 Security Command Center 결과를 **키 없이** 읽게 하는 GCP 쪽 설정이다. 플랫폼 클러스터의 공개 ServiceAccount 발급자(AWS GuardDuty 수집 역할이 믿는 것과 같은 발급자)를 workload identity pool로 믿고, 읽기 전용 reader 서비스 계정을 가장한다. 서비스 계정 키는 만들지 않는다.

| 자원 | 내용 |
|---|---|
| API | `iam` · `iamcredentials` · `sts` · `securitycenter` · `securitycentermanagement`. destroy해도 끄지 않는다(다른 스택이 쓸 수 있다) |
| workload identity pool `bravo-platform` · provider `platform-oidc` | 발급자 `issuer_url`(https만). `attribute_condition`이 수집 SA 주체 하나(`system:serviceaccount:bravo-security:bravo-findings-scc-pull`)만 받는다. 같은 발급자의 다른 주체는 STS에서 거부된다 |
| 서비스 계정 `bravo-scc-reader` | 가장 권한(`roles/iam.workloadIdentityUser`)은 위 주체 하나에만 준다 |
| 프로젝트 역할 | `roles/securitycenter.findingsViewer`(결과 목록), `roles/securitycentermanagement.securityCenterServicesViewer`(탐지 서비스가 켜졌는지: "0건"과 "탐지기 꺼짐" 구분, #925). mute · 상태 변경 · 소스 · 활성화 권한은 없다 |
| 프로젝트 고정 | `project_number`가 다르면 계획이 실패한다(`gcp-gke`와 같은 방식) |

## SCC 활성화는 이 스택 밖이다

SCC 활성화(등급 선택)는 콘솔에서 한다. 조직 없는 프로젝트에서 프로젝트 단위로 켤 수 있는 등급은 샌드박스 권한에 따라 다르므로 켜기 전에 확인한다. 이미 켜져 있는지는 아래로 본다(읽기 전용):

```sh
gcloud services list --enabled --project "$P" --filter="name~securitycenter" --format="value(name)"
gcloud scc manage services describe security-health-analytics --project "$P" --format="value(effectiveEnablementState)"
gcloud iam workload-identity-pools list --location=global --project "$P" --format="table(name,state)"
```

## 펼치기 · 접기

비공개 입력(`vars.json`: `project_id`, `project_number`, `issuer_url`, backend `bucket` · `prefix`)은 Git 밖에 둔다. `gcp-gke`와 같은 상태 버킷을 쓰되 prefix는 따로 둔다.

```sh
export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)
terraform -chdir=infra/envs/gcp-scc-reader init -backend-config=<private>/scc-backend.hcl
terraform -chdir=infra/envs/gcp-scc-reader plan -var-file=<private>/vars.json -out=<private>/scc.tfplan
terraform -chdir=infra/envs/gcp-scc-reader apply <private>/scc.tfplan      # API 5 · pool 1 · provider 1 · SA 1 · IAM 3
terraform -chdir=infra/envs/gcp-scc-reader output -raw scope > <private>/scc-scope.json
terraform -chdir=infra/envs/gcp-scc-reader output -raw credential_configuration > <private>/scc-wif.json
terraform -chdir=infra/envs/gcp-scc-reader output -raw token_audience > <private>/scc-token-audience

terraform -chdir=infra/envs/gcp-scc-reader destroy -var-file=<private>/vars.json
```

출력에는 프로젝트 번호와 서비스 계정 주소가 들어 있어 Git에 두지 않는다. 비밀은 아니다(키가 없다).

## 수집 확인 (운영자 PC에서 한 번)

플랫폼에 `bravo-security` namespace(`infra/envs/platform-security/render.py`)와 ServiceAccount `bravo-findings-scc-pull`이 있어야 한다. 그 SA로 10분짜리 토큰을 받아 같은 판정 경로로 수집한다:

```sh
kubectl -n bravo-security create token bravo-findings-scc-pull --audience "$(cat <private>/scc-token-audience)" --duration 10m > <private>/scc-token
jq --arg f "<private>/scc-token" '.credential_source.file = $f' <private>/scc-wif.json > <private>/scc-wif-local.json
uv run python -m bravo_findings.gcp_pull --environment-id env-gcp --scope-file <private>/scc-scope.json \
  --credentials-file <private>/scc-wif-local.json --database "$PRIVATE_FINDINGS_DB"
```

기대: 탐지 서비스가 켜져 있으면 출처 `OK`(결과가 없으면 0건), 꺼져 있으면 `NOT_ENABLED`, 권한 · 연합이 틀리면 `ACCESS_DENIED`. 어느 실패도 "0건"으로 보이지 않는다.

## 아직

- 플랫폼 주기 수집(CronJob · SA를 `services/security-findings/deploy/platform-gitops`에, GuardDuty 수집과 같은 방식)
- SCC 활성화와 실측 증빙
