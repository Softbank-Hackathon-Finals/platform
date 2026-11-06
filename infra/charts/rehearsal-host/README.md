# T15 자격 이전 리허설 호스트

[`credential_host`](../../../services/platform-events/deploy/approval/REHEARSAL-SSO.md#저장된-최초-제안을-쓰는-자격-이전-호스트)를
플랫폼 `platform-state`에 단일 replica로 실행한다. 저장된 최초 AI 제안만 소비하고 모델을 호출하지 않는다.
argocd 차트의 `rehearsalHost.enabled`는 기본 비활성이다. bootstrap의 보호 runtime 입력으로만 켠다.

## 보호 입력

이미지는 `environments/development/rehearsal-host-image.json`의 발행 digest만 쓴다.
runtime 입력에는 기존 객체 이름만 넣는다. bootstrap은 모두 존재하는지 확인하며 아무것도 만들지 않는다.

| 값 | 내용 |
|---|---|
| `configuration.existingConfigMap` | `host.json`, `ports.json`, `grants.json`, `workload-jwks.json`, `profile.env`, `openbao-ca.crt`, `manager-registration.json`, `runner-registration.json`, `manager-kubeconfig`, `runner-kubeconfig`. `/rehearsal`에 mount한다. registration은 `access/manager.json`, `access/runner.json`로 들어간다. kubeconfig는 그 경로와 `/app/infra/rehearsal-access/credentials.py`로 만든다(`credentials.py kubeconfig`). |
| `configuration.sha256` | 위 ConfigMap data의 정렬 JSON 해시. 변경 시 Pod를 교체한다. |
| `reader.*` | Product Reader의 설정 ConfigMap과 catalog PVC. `ports.json`의 `reader_config`는 `/product-read/...` 경로다. |
| `recordsClaim` | `host.json`의 `records_root=/records`. 이 호스트만 쓰는 Retain PVC다. |
| `secrets.host` | `record_key`, `tls_crt`, `tls_key`. OpenBao `t15/host` 항목을 추가할 때까지 운영자가 만드는 임시 Secret이다. |
| `secrets.catalogKey`, `database`, `databaseCA` | Reader와 같은 기존 Secret. |

SA `t15-rehearsal-host`는 기본 토큰을 받지 않는다. RBAC도 없다.
`/var/run/secrets/t10-intake/token`에 audience `bravo-t10-rehearsal-intake`인 600초 projected token만 받는다.
목적지 접근은 T7 kubeconfig 두 개로만 한다. `t15-manager`는 namespace와 T3, `t15-runner`는 검증 Job에 쓴다.

## 설치 순서

1. 목적지에 `t15-manager` 경계를 설치하고 T7 발급을 등록한다([rehearsal-access README](../../envs/rehearsal-access/README.md#별도-관리-계정-t15-manager)).
2. GitOps는 NetworkPolicy를 관리하지 않는다. 그래서 [rehearsal-host-network.yaml](../../envs/rehearsal-access/rehearsal-host-network.yaml)을 다른 T7 tailnet 정책과 함께 적용한다. 이 파일은 `platform-state` egress와 `tailscale-system` ingress를 담는다.
3. 이미지를 발행하고 pin PR을 병합한다. 보호 입력 객체를 만든다.
4. bootstrap `--runtime-values`에 `rehearsalHost`를 넣어 적용한다. Argo Synced/Healthy를 확인하고, 기동 로그의 두 클라이언트 신원과 cluster UID 대조를 확인한다.

같은 기록 볼륨에 호스트를 둘 이상 띄우지 않는다. 승인 호스트와 같은 계획을 동시에 실행하지 않는다.
