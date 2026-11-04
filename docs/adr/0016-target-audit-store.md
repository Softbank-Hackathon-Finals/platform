# [ADR-0016] 목적지 감사 로그 저장소: SeaweedFS Object Lock 버킷의 OTLP JSON 객체

* **상태 (Status):** 제안됨(Proposed)
* **날짜 (Date):** 2026-11-04
* **관련 (Related):** T34 #56(할 일 3 "목적지 로그 수집 경로"), T37 #104(할 일 6 "목적지 Collector를 T34 로그 파이프라인과 하나로 둘지"), T9 #13(SeaweedFS), T30 #17(목적지 워크로드), T14 #21(AUD-01 검색), T35 #57(목적지 백업 삭제 제한), [ADR-0010](0010-observability-boundary.md)

## 1. 배경 및 문제 상황 (Context and Problem Statement)

* **상황:** [ADR-0010](0010-observability-boundary.md) B2에 따라 원문 감사 로그는 발생 환경 안에 둔다. 출발지는 CloudWatch Logs를 쓴다. 목적지에는 감사 전달 구성(#117, [audit-forwarding](../../services/control-runner/docs/audit-forwarding.md))과 OTLP 감사 디코더(#124, [otlp-audit](../../services/control-runner/docs/otlp-audit.md))가 있지만, 기록을 받아 보존하고 검색하게 해 줄 **저장소는 정하지 않았다.** 지금까지의 실측은 시험용 파일 수신기를 썼다.
* **문제:** 디코더 계약이 저장소에 요구하는 것이 있다.
  * 저장소가 보존을 확인한 시각(`received_at`)과 불변 영수증(`receipt_id`)을 준다.
  * 쓰기 주체와 검색 주체를 나누고, 검색 주체는 쓰거나 지울 수 없다.
  * 시간 창의 모든 페이지를 끝까지 읽을 수 있고, 잘리면 알 수 있다.
  * 온프렘 단일 노드에서 앱 · CNPG · PgBouncer · SeaweedFS · OpenBao와 함께 돈다.

  같은 결정으로 T37 할 일 6, 즉 목적지의 지표 Collector(T37)와 감사 전달기(T34)를 합칠지도 정해야 한다.
* **목표:** 목적지 감사 기록을 지우거나 바꿀 수 없게 보존한다. 쓰기와 검색 권한을 나누고, 디코더 계약을 그대로 만족한다. 목적지에 이미 들어오는 구성요소만 쓴다.

## 2. 고려한 옵션들 (Considered Options)

### 1. SeaweedFS Object Lock 버킷에 OTLP JSON 객체로 저장 (Collector `awss3` exporter)

목적지 SeaweedFS(T9 · T30이 4.52를 고정)에 감사 전용 버킷을 Object Lock으로 만들고, 기본 보존을 COMPLIANCE로 둔다. 감사 전달기는 OTel Collector 0.164.0의 `awss3` exporter로 분 단위 경로(`audit/<환경>/YYYY/MM/DD/HH/MM/`)에 OTLP JSON 객체를 쓴다. 키는 uuidv7이다.

**Pros**
* 새 서버 소프트웨어가 없다. SeaweedFS와 Collector는 목적지에 이미 들어온다.
* 저장소가 영수증을 준다. `received_at`은 LastModified, `receipt_id`는 키 + 버전 ID다.
* 쓰기 전용 · 읽기 전용 권한과 COMPLIANCE 보존을 이 버전에서 실측했다(아래 4절).
* 검색은 버전 목록(ListObjectVersions)의 페이지 이어 읽기라서, 잘렸는지 판단이 명확하다.
* T35의 목적지 백업 삭제 제한도 같은 장치(Object Lock + 버킷 정책)로 만든다.

**Cons**
* 전문 검색 색인이 없다. 시간 창(10분) 안의 객체를 모두 읽어 거른다. 데모 규모에서는 충분하지만 대량 로그 검색 도구는 아니다.
* `awss3` exporter는 alpha다. 판(0.164.0)을 고정하고 실측으로 확인한다.
* 쓰기 주체가 조건 없이 같은 키에 쓰면 새 버전이 생기고, 관리자의 삭제 표시는 일반 목록에서 객체를 숨긴다. 원본 버전은 남지만 검색 어댑터가 이를 변조 흔적으로 다뤄야 한다(3절).
* COMPLIANCE 보존은 S3 계층의 보호다. 노드 루트 권한으로 볼륨 파일을 지우는 것까지 막지는 못한다.

### 2. Grafana Loki (단일 바이너리, 파일시스템)

**Pros**
* 로그 검색 언어와 레이블 색인이 있어 대량 검색에 강하다.
* OTLP 수신을 지원한다.

**Cons**
* 질의 결과가 사건 시각 기준이다. 디코더 계약의 `received_at` · `receipt_id`(저장소가 보존을 확인한 시각과 불변 영수증)를 주려면 별도 구현이 필요하다.
* 자체 인증이 없어 쓰기 · 검색 권한 분리를 앞단 게이트웨이로 만들어야 한다.
* 목적지 단일 노드에 서버 하나가 더 늘고, 보존은 설정 기반 삭제라 WORM 보장은 아니다.

### 3. VictoriaLogs (단일 바이너리)

**Pros**
* 자원이 가볍고 OTLP를 받는다.

**Cons**
* Loki와 같은 이유로 영수증 계약을 따로 구현해야 하고, 권한 분리 · 변경 불가 보존을 별도로 만들어야 한다.
* 팀이 아직 쓰지 않는 구성요소가 하나 더 생긴다.

### 4. OpenSearch

**Cons**
* JVM 기반이라 메모리 요구가 커서 목적지 단일 노드 예산에 맞지 않는다. 역할 기반 권한은 강점이지만 이 규모에 과하다.

### 5. Collector 파일 exporter (지금의 시험 수신기)

**Cons**
* 검색 API · 권한 분리 · 변경 불가 보존이 없다. 시험용으로만 쓴다.

## 3. 결정 사항 (Decision Outcome)

* **옵션 1(SeaweedFS Object Lock 버킷)을 제안한다.**
* **옵션 2 · 3은 검색 규모가 커지면 다시 본다.** 영수증 계약을 함께 구현해야 한다.
* **옵션 4 · 5는 채택하지 않는다.**
* **T37 할 일 6: 목적지 지표 Collector(T37)와 감사 전달기(T34)는 합치지 않는다.** 같은 이미지 digest와 버전 정책만 공유한다.

**저장소 구성 (T9 · T30 적용 제안)**

| 항목 | 값 |
|---|---|
| 버킷 | 감사 전용(예: `bravo-audit`), 생성할 때 Object Lock 활성(버전 관리 자동) |
| 기본 보존 | COMPLIANCE. 기간은 운영자가 정한다(데모 제안 7일). 관리자도 기간 안에는 버전을 지우지 못한다 |
| 쓰기 identity | 그 버킷 `Write`만. 버킷 정책으로 삭제 · 버전 삭제 · 버전 설정 · 보존 변경 · 정책 변경을 Deny |
| 검색 identity | 그 버킷 `Read` · `List`만. 버킷 정책으로 쓰기 · 삭제를 Deny |
| 관리 identity | 버킷 · 보존 · 정책 생성에만 쓰고 배포 자격으로 나눠 주지 않는다 |
| 전송 | HTTPS(목적지 CA), path-style, SigV4. 익명 요청 거부 |
| 목적지 밖 연결 | 없다. 전달기와 저장소가 같은 환경 안에 있다. T14 검색 경로는 T12 방식의 tailnet ingress 또는 목적지 안 실행을 T30 · T12와 정한다 |

**검색 어댑터 규칙 (T34 후속 구현)**

1. 시간 창에 걸친 분 단위 경로를 `ListObjectVersions`로 끝까지 읽는다. 페이지가 잘리거나 예산을 넘으면 UNKNOWN이다.
2. 한 키에 버전이 둘 이상이거나 삭제 표시가 있으면 변조 흔적으로 보고 UNKNOWN이다. 일반 목록(`ListObjectsV2`)만으로는 판정하지 않는다.
3. `received_at`은 LastModified + 1초(초 단위 내림의 상한)로 쓴다. 수집 기한 판정에서 보수적인 쪽이다.
4. `receipt_id`는 키 + 버전 ID다. 생산자 출처(`AuditOrigin`)는 쓰기 identity의 등록에서 정하고, 객체 내용의 속성으로 정하지 않는다.
5. 객체마다 `otlp_audit.decode_export`를 거쳐 기존 `AuditSearchProbe`가 판정한다.

**지표 Collector와 나누는 이유**

| 기준 | 지표 Collector (T37) | 감사 전달기 (T34) |
|---|---|---|
| 목적지 | 플랫폼 지표 저장소(환경 밖, mTLS) | 같은 환경의 감사 버킷(환경 안) |
| 원천 접근 | 앱 OTLP · DB 지표 포트. hostPath 없음, restricted | 노드의 CNPG 로그 · RKE2 감사 파일을 읽기 전용 hostPath로 |
| 실패 처리 | 디스크 큐 10분 재시도, 넘치면 버림(관측은 확인 불가로 표시) | 영속 큐 · 만료 없는 재시도. 버리면 안 된다 |
| 신뢰 라벨 | `bravo_signal`(지표 출처) | 등록된 Pod · 노드 UID와 쓰기 identity |

합치면 지표 Collector에 노드 파일 접근 권한이 붙고, 감사 전달의 역압이 지표 생존 신호를 막을 수 있다. 실패 정책도 서로 반대라 한 프로세스에 두지 않는다.

**이유**
* 디코더 계약(영수증 · 수신 시각 · 권한 분리 · 완전한 검색)을 저장소 기능만으로 만족하고, 그 성질을 고정 버전에서 실측했다.
* 목적지에 새 서버를 늘리지 않는다. T9 · T30이 이미 고정한 SeaweedFS와 T34가 고정한 Collector를 쓴다.
* ADR-0010 B2(로그는 환경 안)와 "목적지는 바깥 방향 연결만" 제약을 지킨다.

## 4. 결과

**실측 (로컬 Docker, 2026-11-04)** — [`verify_target_audit_store.py`](../../services/control-runner/tests/integration/verify_target_audit_store.py), [증빙](../evidence/t34-target-audit-store-2026-11-04/README.md). SeaweedFS 4.52(T30과 같은 digest), Collector 0.164.0(T34와 같은 digest), 사설 CA HTTPS, 외부 연결 없는 내부 네트워크.

| 확인 | 결과 |
|---|---|
| Object Lock 버킷 · COMPLIANCE 기본 보존 · 버전 관리 | 생성 · 적용됨 |
| 쓰기 identity | PUT 성공, 같은 키 조건부 PUT 412. 삭제 · 버전 삭제 · 버전 중지 · 정책 삭제 · 읽기 · 목록 모두 403 |
| 검색 identity | 목록 · 버전 목록 · 버전 읽기 성공. PUT · 삭제 403 |
| 관리 identity | 잠긴 버전 삭제 403 |
| 익명 | 목록 403 |
| Collector `awss3` → HTTPS | OTLP JSON 객체 1개, 기록 1건, COMPLIANCE 보존 적용, 관리자 버전 삭제 403, 오류 로그 없음 |
| LastModified | PUT 시각을 1초 안에서 감쌈(초 단위) |
| 함정 | 조건 없는 덮어쓰기는 버전 추가, 관리자 삭제 표시는 일반 목록에서 숨김. 두 경우 모두 원본 버전은 그대로 |

**k3s 시제품 (2026-11-04)** — [`templates/target-audit-store`](../../services/control-runner/templates/target-audit-store/)를 Docker 안 k3s v1.35.9(목적지 RKE2와 같은 쿠버네티스 버전)에 적용, [`verify_target_audit_store_k8s.sh`](../../services/control-runner/tests/integration/verify_target_audit_store_k8s.sh) 6/6 통과.

| 확인 | 결과 |
|---|---|
| 배포 | 저장소(T30과 같은 SeaweedFS 인자 + S3 TLS) · 전달기가 restricted 네임스페이스에서 준비됨 |
| 버킷 구성 | `-s3.iam=false`에서도 Object Lock · COMPLIANCE 보존 · 버킷 정책 적용 |
| 전달 · 검색 | source Pod의 OTLP 기록이 전달기 → HTTPS → 저장소에 남고, 검색 Pod가 검색 키만으로 버전 목록을 끝까지 읽음(변조 흔적 없음, PUT 거부) |
| 권한 · 함정 | Docker 실측과 같음 |
| 네트워크 | 저장소 8333은 역할 라벨(전달기 · 검색 · 초기 구성) Pod만. 라벨 없는 Pod와 다른 네임스페이스 Pod는 닿지 않음 |

**긍정적 결과**
* T34 할 일 3의 저장소가 정해지고, T37 할 일 6이 "합치지 않음"으로 닫힌다.
* T35 목적지 백업의 "앱 · 실행 역할로 백업 삭제 불가"를 같은 장치로 만들 수 있다.

**부정적 결과 · 남은 일**
* 검색 어댑터(3절 규칙)를 구현하고, 실제 목적지 RKE2에서 카나리 3종(관리자 로그인 · DDL · RoleBinding)을 10분 안에 검색해야 T34가 끝난다.
* T9 · T30에 버킷 · identity · 정책 · 보존 적용을 요청한다. 보존 기간과 디스크 상한(볼륨 quota), 수집 중단 알림은 운영 설정으로 정한다.
* COMPLIANCE 보존 동안에는 데모 정리에서도 버전을 지울 수 없다. 정리는 볼륨 단위로 한다.
* 시제품 저장소는 emptyDir이다. 목적지 적용 때 영속 볼륨 · 용량 상한과 노드 로그를 읽는 운영 전달기(#117 `collector_config`, 영속 큐)를 붙인다.
