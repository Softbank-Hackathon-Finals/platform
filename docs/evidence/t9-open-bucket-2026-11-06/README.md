# T9 데모용 열린 버킷 상태: 열기 · 닫기 · 재시작 필요 여부 로컬 실측

2026-11-06 14:29:58–14:41:30 KST, 커밋 `a4cd3d0`(원격 브랜치 `feature/T9-open-bucket-measure`에 푸시한 뒤 실행)의
[`open_bucket_kind.py`](../../../infra/envs/target-workloads/testing/open_bucket_kind.py)로 측정했다.
**로컬 kind 클러스터다. 데모 목적지(RKE2)가 아니다.** [readback](readback.json)은 비공개 원본 결과·실행 로그의
해시와 사례별 결과·시간을 담는다. 자격 증명·객체 본문은 기록하지 않았다. [source-manifest](source-manifest.json)는
측정 커밋의 렌더러·점검 스크립트·시험 파일 해시이며, 실행 결과에 기록된 해시와 같다(측정 소스 미커밋 변경 없음).
**49개 사례 모두 기대대로**였다.

## 환경

| 항목 | 값 |
| --- | --- |
| kind | v0.32.0, 클러스터 `t9-open`, 단일 노드 `kindest/node@sha256:3489c767…`, Kubernetes v1.36.1, containerd 2.3.1, arm64 |
| SeaweedFS | `objects.py`의 4.52 index digest `57877e32…`. Pod `imageID`도 같은 digest |
| 적용한 렌더 | `render.py`의 Namespace(PSS restricted)·StorageClass·ServiceAccount, `objects.py`의 PV·PVC·Service·Deployment를 그대로. `s3.json`은 Secret `target-seaweedfs-auth`를 렌더대로 읽기 전용 마운트(`items`, subPath 아님) |
| 적용하지 않은 것 | `objects.py`의 NetworkPolicy 두 개. 호스트에서 재려고 시험 전용 NodePort(30833 → 호스트 127.0.0.1:18333)를 더했다 |
| `s3.json` | `object_exposure.render`로 실행마다 만든 무작위 자격. `--rehearsal-bucket rehearsal-attachments`를 함께 줘서 다른 버킷 대조군을 만들었다. closed identity: bootstrap · client · rehearsal-object-admin, open-demo: 여기에 `anonymous`(`Read:demo-chat-attachments`) |
| Secret 교체 | `plan`의 `kubectl create secret … --dry-run=client -o json \| kubectl apply -f -`와 같은 객체를 stdin으로 apply |
| 요청 | 익명은 서명 없는 HTTP, client · bootstrap은 SigV4 서명 요청(path style, `us-east-1`). 대상 객체 `demo-chat-attachments/t9/open-bucket-probe.txt` |

## 결과

### 재시작 없이는 바뀌지 않는다

| 전환 | Secret 교체 | 마운트 파일이 새 바이트가 된 시점 | 재시작 없이 관측 | `rollout restart` 뒤 |
| --- | --- | --- | --- | --- |
| closed → open-demo | 14:30:53 | +83.2초 | 174.7초 동안 2초 간격 85회 익명 GET 모두 **403**. 파일이 바뀐 뒤 91.5초 더 봐도 그대로. Pod UID·재시작 횟수 그대로 | `rollout status` 완료 35.6초, 익명 GET **200**까지 37.6초 |
| open-demo → closed | 14:34:30 | +56.2초 | 147.6초 동안 72회 익명 GET 모두 **200**. 파일이 바뀐 뒤 91.4초 더 봐도 그대로 | `rollout status` 완료 35.8초, 익명 GET **403**까지 37.8초 |

kubelet은 마운트된 Secret 파일을 1분 안팎에 바꾸지만, 실행 중인 SeaweedFS는 그 파일을 다시 읽지 않는다.
**`rollout restart`가 필요하다.** Deployment가 `Recreate`라 기존 Pod가 먼저 내려가고 새 Pod가 Ready가 될 때까지
(`rollout status` 기준 약 36초) S3 응답을 기대할 수 없다(응답 없는 구간 자체는 따로 재지 않았다). 이번 측정에서는 파일이 이미 바뀐 뒤에 재시작했다. Secret 교체 직후 바로 재시작하는 경우를 따로 재지는 않았다
(새 Pod는 시작할 때 현재 Secret을 마운트한다).

참고(운영 순서 아님): 파일이 바뀐 뒤(+87.1초, 익명 GET 여전히 403) Pod 안에서 `kill -HUP 1`을 보내자 첫 관측(0.0초)에서
익명 GET이 200이 됐고 Pod는 그대로였다. 4.52는 SIGHUP에 `s3.json`을 다시 읽는다. 이 방법은 `pods/exec` 권한과
kubelet 반영 대기가 필요하므로 운영 순서는 재시작으로 둔다.

### open-demo에서 익명이 할 수 있는 것

| 요청(익명) | 결과 |
| --- | --- |
| 첨부 버킷 객체 GET | **200**, 본문이 올린 바이트와 같음. HEAD 200 |
| 같은 버킷 PUT | 403, 그 키는 생기지 않음(client GET 404) |
| 같은 버킷 LIST(v1 · `list-type=2`) | 403 |
| 같은 버킷 객체 DELETE | 403, 객체는 그대로(client GET 같은 바이트) |
| 다른 버킷(`rehearsal-attachments`) 객체 GET · LIST | 403 |
| 버킷 정책 쓰기(`PUT ?policy`) | 403 |
| 새 버킷 생성 | 403 |
| ListBuckets(`GET /`) | 403, 버킷 이름 없음 |

client는 open-demo에서도 GET(같은 바이트) · PUT이 그대로 됐다. closed에서 익명 GET은 첨부 버킷 · 다른 버킷 모두 403,
client GET은 200이었다.

### 남은 버킷 정책은 closed `s3.json`을 무력화한다

설치 직후(closed)와 open-demo의 첨부 버킷에는 버킷 정책이 없었다(bootstrap `GET ?policy` → 404 `NoSuchBucketPolicy`).
closed로 되돌린 뒤 bootstrap이 `Allow` · `Principal "*"` · `s3:GetObject` 정책을 붙이자:

| 관측 | 결과 |
| --- | --- |
| 익명 GET | 첫 관측(0.0초)부터 **200**. 마운트 파일은 closed 해시 `d516004c…` 그대로 |
| `rollout restart` 뒤(37.8초) | 여전히 **200**. 정책은 filer에 남아 재시작으로 사라지지 않는다 |
| 익명 LIST · 다른 버킷 GET | 403 (정책 범위 밖) |
| bootstrap `DELETE ?policy` 뒤 | 첫 관측(0.0초)부터 익명 GET **403**, client GET 200, `GET ?policy` 404 |
| client의 정책 쓰기 · 삭제 | 403 (bootstrap의 Admin만 가능) |

T31 [kind 실측](../t31-policy-apply-2026-11-06/README.md)의 `ANONYMOUS_READ_STILL_ALLOWED`와 같은 현상이다.
따라서 데모를 리셋하거나 컷오버 전에 닫을 때는 `s3.json` closed와 함께 **첨부 버킷의 버킷 정책이 없음(404)**을
확인한다. 남아 있으면 bootstrap으로 지운다.

### `object_exposure.py probe` 분류

호스트에서 CLI로 실행한 결과가 매번 실제 상태와 맞았다: 설치 직후 `closed`, open-demo `open`, 다시 닫은 뒤 `closed`,
남은 정책 `open`(실제로 열려 있으므로 맞음), 정책 삭제 뒤 `closed`, 마지막 `closed`. probe는 원인을 구분하지 않는다.
`open`이 나왔는데 `s3.json`이 closed라면 버킷 정책을 먼저 의심한다.

## 어디까지 확인했나

| 범위 | 상태 |
| --- | --- |
| 렌더 그대로의 SeaweedFS 4.52 Deployment에서 Secret 교체 · 재시작 · 익명/서명 요청 | 확인 (kind) |
| `objects.py` NetworkPolicy가 적용된 경로 | 미적용 (호스트 시험용 NodePort) |
| 데모 목적지 RKE2에서 열고 닫기 | **미측정** — 목적지 운영자와 시간 조율 필요 |
| 게이트 IAM-01.1이 open-demo에서 BLOCK, closed에서 PASS를 내는지 | 미측정 (이 스크립트는 상태만 관측) |

## 재현

```sh
python3 infra/envs/target-workloads/testing/open_bucket_kind.py --print-kind-config > <임시>/kind.yaml
kind create cluster --name t9-open --kubeconfig <임시 kubeconfig> --config <임시>/kind.yaml
python3 infra/envs/target-workloads/testing/open_bucket_kind.py --kubeconfig <임시 kubeconfig> --output <비공개 경로>/results.json
kind delete cluster --name t9-open
```

스크립트는 `kind-*` 단일 노드 컨텍스트이고 `target-dev` namespace가 없을 때만 실행된다. 노드 경로 준비에 `docker exec`를
쓴다. 실행 뒤 kind 클러스터를 지웠다. 비공개 원본(결과·실행 로그)은 저장소 밖 접근 제한 경로에 남겼다.
