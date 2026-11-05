# T8 첨부 복사 Job 임시 k3s 실측 (2026-11-06)

렌더한 Job은 임시 k3s에서 첨부를 복사했고, 판정 명령은 성공 · 실패 · 미확인을 구분했다. **실제 AWS · RKE2 · Tailscale · 대상 VM에서는 재지 않았으므로 T8의 첨부 동기화 할 일은 아직 끝나지 않았다.**

클라우드 자원은 호출하지 않았다. 숫자는 모두 스크립트가 실행 결과에서 만든 [result.json](result.json)과 [mutations.json](mutations.json)에서 왔다. 실행 방법은 [운영 안내](../../operations/t8-attachment-copy-job.md)에 있다. 잰 코드는 result.json의 `source_revision`이다.

## 실제와 대역

| 항목 | 이번 실행 | 실제와 다른 점 |
| --- | --- | --- |
| 클러스터 | k3s 1.35.5, Pod Security `restricted` 강제, kube-router NetworkPolicy | 실제는 RKE2 1.35.9 + Canal |
| Job · 이미지 | 저장소 렌더 결과, `attachment-copy.Dockerfile`로 빌드한 digest | 로컬 arm64 빌드. 실제는 amd64 |
| 목적지 | `objects.py`의 이미지 digest · 인수 · PV · NetworkPolicy, 버킷 범위 identity 2개 | 에뮬레이션(amd64 on arm64). TLS 없음 |
| 출발지 S3 | **대역: MinIO.** `hostAliases`로 실제 S3 이름을 가리키고 시험 CA를 SDK 번들 자리에 마운트. Job의 환경 변수는 렌더 그대로라 HTTPS · 가상 호스트 주소 · 서명을 실제 코드 경로로 지난다 | IAM · KMS · `ExpectedBucketOwner` 거부는 재지 못한다 |
| 세션 | **대역: MinIO STS.** 렌더한 정책의 S3 허용과 버킷 거부만 적용 | KMS 허용과 `NotAction` 거부는 평가되지 않았다 |
| DB 경로 | **대역: selector 없는 Service → PostgreSQL 18.4 컨테이너.** 저장소 `schema.sql`, 실제 `chat_replicator` 권한, `verify-full` | Tailscale 경로가 아니다 |
| Secret 생성 | 운영 안내의 `jq | kubectl` 명령을 문서에서 읽어 실행 | 앞의 `aws sts` 한 줄만 MinIO 응답으로 바꿨다 |

## 잰 것

26개 점검이 모두 통과했다(285초).

| 경우 | 결과 |
| --- | --- |
| 첫 실행 (한글 · 공백 키, 이진 본문, 빈 객체) | `SUCCEEDED`, 종료 코드 0, `copied` 3, 1030바이트 |
| 목적지 바이트를 영수증과 별도로 읽어 비교 | 3개 일치 |
| 재실행 | `SUCCEEDED`, `reused` 3, 해시 동일 |
| 출발지 객체 | ETag · 수정 시각 불변 |
| DB에 없는 객체가 출발지에 있음 | `FAILED`, 종료 코드 1, 목적지 쓰기 없음 |
| 그 객체를 개별 승인 (`--with-approved-extras`) | `SUCCEEDED`, DB 3 + 승인 1, 양쪽 객체 수 4개로 같음 |
| 복사 전에 프로세스 충돌 | `UNKNOWN`, 종료 코드 2, 로그 0바이트 |
| 세션 토큰 없는 Secret | 컨테이너가 시작하지 않음(`CreateContainerConfigError`) |
| 그 상태로 시간 초과 | `UNKNOWN`, `job_failure_reason` = `DeadlineExceeded` |
| 성공한 실행의 Pod를 지움 | `UNKNOWN` (`pod_count_not_one`) |
| 없는 Job, 다른 이미지 digest, 다른 plan_id | 모두 `UNKNOWN` |
| 라벨 없는 Pod (대조군) | 목적지에 닿지 못해 복사가 시작되지 않음, `UNKNOWN` |
| 렌더한 Pod | `restricted` 통과. 대조군 `allowPrivilegeEscalation: true`는 거부 |
| 세션 자격 | 읽기 성공, 출발지 쓰기 · 삭제 · 다른 버킷 거부 |
| DB 접속 | `chat_replicator`로 TLS 접속 5회. 같은 역할의 행 삭제는 권한 없음 |
| 로그 · 판정 출력 9KB, 렌더 매니페스트, 저장된 Job | 자격 · 객체 키 · 버킷 · 계정 값 없음 |

### 복사 전 대기가 필요한 이유

첫 구현은 Job 3개가 모두 `EndpointConnectionError`로 실패했다. 보조 Pod는 SDK 재시도 덕분에 통과해서 원인이 가려졌다.

| 측정 (새 Pod, 라벨 있음) | 결과 |
| --- | --- |
| 복사 코드의 목적지 클라이언트로 시작 직후부터 반복 요청, 3회 | 첫 요청 3회 모두 거부. 0.98초, 1.24초, 0.58초에 성공 |
| 소켓 연결 반복, 3회 | 1.65초, 0.21초, 0.41초에 열림. 그 전은 `ConnectionRefusedError` |
| 지연 뒤 한 번만 소켓 연결 | 0초 · 0.5초 거부. 1 · 2 · 4 · 8초 성공 |
| 대기 없이 돌린 복사 (대조군) | `FAILED`, `EndpointConnectionError` |

그래서 `wait-for-target` init 컨테이너가 같은 Pod에서 연결을 확인한 뒤 복사를 시작한다. 거부되는 까닭은 NetworkPolicy가 새 Pod 주소를 늦게 반영해서라고 추정하며, 직접 확인하지는 않았다. RKE2와 Canal에서 이 시간이 얼마인지는 모른다.

### 일부러 검사를 뺀 결과

렌더러 · 판정기 · 이미지 파일에서 안전 속성을 하나씩 뺀 복사본 127개를 시험 83개로 돌려 모두 잡았다. 첫 실행에서는 113개 중 6개가 살아남았다. 5개는 시험에 없던 경우(잘못된 Job UID, Job 조건과 영수증이 어긋나는 세 경우, 실패한 kubectl의 출력)여서 시험을 추가했고, 1개는 변이가 들여쓰기를 깨서 재지 못한 것이라 변이를 고쳤다. 지금은 시험 함수가 실제로 실패해야만 잡힌 것으로 센다.

### 고친 결함

Dockerfile의 `COPY --chmod=0444`가 `/app`을 검색 비트 없이 만들어 비루트 사용자가 모듈을 가져오지 못했다. 이미지 시험에서 발견해 `WORKDIR`을 앞에 두고 시험을 추가했다.

## 재지 않은 것

- **실제 S3 · IAM · KMS:** `GetFederationToken`, 세션 정책의 KMS 허용과 `NotAction` 거부, KMS 복호화, 틀린 소유자 거부. 운영자 IAM 사용자에게 이 권한이 있는지도 확인하지 않았다.
- **RKE2 · Canal · 대상 VM:** NetworkPolicy 반영 시간, amd64 이미지, 대상 VM의 digest 가져오기, 대상 VM에서 S3로 나가는 HTTPS. 런타임이 보고하는 `imageID` 형식은 k3s에서만 확인했다.
- **Tailscale:** DB 경로의 처리량 · 지연. 이 경로에서의 복사 시간은 재지 않았으므로 10분 제한은 추정값이다.
- **실제 데이터 크기:** 이번 복사는 1030바이트다. 100개 · 16MiB 한도는 이전 [로컬 측정](../t8-attachment-sync-2026-11-04/README.md)으로만 쟀다.
- **T9 이후 HTTPS 목적지:** Job은 지금의 HTTP 주소를 쓴다.
- **T12 연결:** 결과 JSON을 읽는 코드는 없다.
