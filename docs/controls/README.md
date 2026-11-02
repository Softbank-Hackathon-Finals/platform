# 6개 통제 시험 설계 (T5)

설계 문서 8.5의 6개 통제를 출발지(AWS EKS)와 목적지(온프렘 RKE2)에서 **같은 의미로** 시험하는 방법, 합격 기준, UNKNOWN 조건, 게이트 질문, 결과 · 증거 형식을 고정한다. 통제 시험기(T14)가 이대로 시험하고, 원본 대조 게이트(T19)와 콘솔(T21)이 결과를 읽는다.

| 파일 | 내용 |
|---|---|
| 이 문서 | 하위 시험 · 통제 ID 매핑 · 판정 규칙 · UNKNOWN 조건 · 게이트 질문 |
| [`control-result.schema.json`](control-result.schema.json) | 결과 · 증거 형식 계약 (JSON Schema 2020-12) |
| [`examples/`](examples/) | 데모 게이트 질문 3개의 예시 데이터. 고치기 전(BLOCK)과 고친 뒤(PASS). T19 · T21 모의 데이터로 쓴다 |

## 1. 공통 원칙

- **같은 의미로 시험한다.** 도구가 달라도(Security Group ↔ NetworkPolicy, Secrets Manager ↔ OpenBao) 같은 질문에 같은 방식으로 답하게 맞춘다. 시험 방법은 `method_version`으로 관리하고, 양쪽 버전이 다르면 비교하지 않는다(UNKNOWN).
- **거부 시험에는 허용 대조군을 붙인다.** "다른 주체는 거부된다"를 보기 전에 "정상 주체는 된다"를 먼저 확인한다. 대조군이 실패하면 서비스 장애나 시험 준비 실패이므로 거부를 PASS로 세지 않는다(UNKNOWN).
- **"거부"는 정해진 응답으로만 인정한다.** 시험마다 거부로 볼 응답(예: `AccessDenied`, HTTP 403, `permission denied`)을 정해 두고, 그 외의 응답(500, 타임아웃, 연결 끊김)은 UNKNOWN이다.
- **"관찰하지 못함"과 "관찰했더니 없음"을 나눈다.** 검색 · 조회 자체를 못 했으면 UNKNOWN, 검색은 됐는데 있어야 할 것이 없으면 FAIL이다.
- **판정은 규칙이 한다.** LLM을 쓰지 않는다. 점수나 개수 하나로 요약하지 않고 시험 ID 단위로 비교한다.
- **증거에 비밀 원문을 남기지 않는다.** 출력은 해시와 마스킹한 발췌만 남긴다.
- **운영 데이터를 건드리는 시험은 전환 창 밖에서 돌린다.** 카나리 객체(테이블, RoleBinding, 표식 행)는 시험 전용 이름 · 스키마에 만들고 시험이 끝나면 지운다.

## 2. 하위 시험

시험 ID는 `<통제 ID>.<번호>`다. 통제 ID는 설계 문서 8.5를 따른다. 증거 유효기간 기본값은 24시간(제안값, 실측 후 조정)이다.

### 비밀 저장 — SEC-01 · ENC-02

| 시험 ID | 질문 | 출발지 (AWS) | 목적지 (온프렘) | 합격 | 허용 대조군 |
|---|---|---|---|---|---|
| SEC-01.1 | 다른 서비스 계정으로 앱 DB Secret을 읽을 수 있는가? | 다른 IRSA 역할로 `secretsmanager:GetSecretValue` | 다른 SA 토큰으로 OpenBao read (ESO 경로) | 거부 (`AccessDenied` / 403) | 앱 SA로 같은 Secret 읽기 성공 |
| SEC-01.2 | Git에 평문 비밀이 있는가? | gitleaks: 앱 레포 + 렌더된 매니페스트 | 같음 | 0건 | — |
| SEC-01.3 | 로그에 비밀이 찍히는가? | 시험용 카나리 비밀을 앱 설정에 넣고 앱 로그(CloudWatch Logs) 검색 | 같은 방식으로 앱 로그(클러스터 로그 수집기) 검색 | 0건 | 같은 로그에서 카나리가 아닌 고유 문자열은 검색됨 |
| SEC-01.4 | 비밀을 1회 회전해도 앱이 재시작 없이 동작하는가? | Secrets Manager 회전 → DB에서 앱 역할의 **기존 연결 종료** → `/api/health/db` · 쓰기 1건 | OpenBao 회전 + ESO 동기화 → 같음 | 30초 안에 `/api/health/db` 200, 쓰기 201 (그사이 503은 허용) | 회전 전 `/api/health/db` 200 · 쓰기 201 |
| ENC-02.1 | 비밀 저장소가 암호화돼 있는가? | Secret에 연결된 KMS 키 확인 | RKE2 secrets-encryption 상태, OpenBao seal 상태 | 켜짐 | — |

SEC-01.4: 기존 연결은 비밀번호가 바뀌어도 유지되므로, 기존 연결을 끊어 **새 연결이 새 비밀번호로 붙는지**를 본다. `/readyz`는 DB를 보지 않으므로(demo-chat#5) 이 시험에 쓰지 않는다. demo-chat은 인증 실패 시 Secret을 다시 읽어 재시작 없이 회복한다(로컬 실측 1초). 목적지에서 ESO로 주입하는 방식으로 바뀌면(T31) 같은 시험으로 회복 여부를 다시 본다.

### 전송 암호화 — ENC-01

| 시험 ID | 질문 | 출발지 | 목적지 | 합격 | 허용 대조군 |
|---|---|---|---|---|---|
| ENC-01.1 | 앱 → pooler 구간이 인증서 검증으로 연결되는가? | 앱 네임스페이스에서 pooler로 `psql sslmode=verify-full` | 같음 | 연결 성공, TLS 1.2+ | — |
| ENC-01.5 | pooler → DB 구간이 인증서 검증 TLS인가? | PgBouncer `server_tls_sslmode`(= `verify-full`) 설정과 DB의 `pg_stat_ssl`에서 pooler 연결의 `ssl = true` | 같음 | 둘 다 만족 | — |
| ENC-01.2 | 틀린 CA · hostname으로 연결되는가? | 다른 CA / 다른 hostname으로 `verify-full` | 같음 | 거부 (인증서 검증 실패) | ENC-01.1 성공 |
| ENC-01.3 | 암호화 없이 연결되는가? (다운그레이드) | `sslmode=disable` | 같음 | 거부 (`pg_hba` · pooler 거부) | ENC-01.1 성공 |
| ENC-01.4 | 복제 구간이 TLS인가? | 구독 연결의 `sslmode`와 출발지 `pg_stat_ssl` | 같음 | TLS 사용, `verify-full` | — |

ENC-01.1만으로는 pooler → DB 구간을 볼 수 없다. `psql`은 pooler까지만 연결하기 때문이다. 그래서 ENC-01.5를 따로 둔다.

### 감사 로그 — AUD-01

정해 둔 이벤트를 고유 표식(카나리)과 함께 **실제로 발생시키고**, 정해 둔 시간(기본 10분, 제안값) 안에 검색되는지 본다.

| 시험 ID | 질문 | 출발지 | 목적지 | 합격 |
|---|---|---|---|---|
| AUD-01.1 | 관리자 로그인 이벤트가 검색되는가? | 카나리 `application_name`으로 DB 관리자 접속 → CNPG 로그(`log_connections`)를 CloudWatch Logs에서 검색 | 같은 접속 → CNPG 로그를 클러스터 로그 수집기에서 검색 | 검색됨 |
| AUD-01.2 | DDL 이벤트가 검색되는가? | 시험 전용 스키마에 카나리 테이블 생성 · 삭제 → pgaudit(`ddl` 클래스) 로그 검색 | 같음 | 검색됨 |
| AUD-01.3 | 권한 변경 이벤트가 검색되는가? | 카나리 RoleBinding 생성 · 삭제 → EKS 컨트롤 플레인 `audit` 로그(CloudWatch Logs) 검색 | 같음 → RKE2 kube-apiserver audit 로그 검색 | 검색됨 |

- 접속(로그인)은 pgaudit가 아니라 PostgreSQL `log_connections`가 기록한다. pgaudit는 문장(DDL 등)을 기록한다.
- CloudTrail은 AWS API 호출만 기록하고 K8s 객체 변경은 남기지 않는다. EKS 감사 로그는 컨트롤 플레인 로깅의 `audit` 유형을 켜야 CloudWatch Logs로 나온다.
- 검색 자체를 못 하면(권한 없음, 수집기 없음) UNKNOWN, 검색은 됐는데 이벤트가 없으면 FAIL이다(데모 게이트 질문 3은 UNKNOWN).

### 네트워크 노출 — NET-01

| 시험 ID | 질문 | 출발지 | 목적지 | 합격 | 허용 대조군 |
|---|---|---|---|---|---|
| NET-01.1 | 외부 스캔에 공개 진입점만 보이는가? | 클러스터 밖에서 nmap (노드 · LB 주소) | 같음 (온프렘 VM 주소) | 허용 목록 외 열린 포트 0 | 공개 진입점(Tunnel 경유)은 응답 |
| NET-01.2 | 외부에서 DB(5432 · 6432)에 붙을 수 있는가? | 클러스터 밖에서 TCP 연결 | 같음 | 거부 (연결 거부 · 차단) | 클러스터 안 앱에서는 연결 성공 |
| NET-01.3 | 다른 네임스페이스에서 DB에 붙을 수 있는가? | 시험용 NS의 파드에서 DB 연결 (SG for Pods / NetworkPolicy) | 시험용 NS의 파드에서 DB 연결 (NetworkPolicy) | 거부 | 앱 NS의 시험 파드는 연결 성공 |

NET-01.3: EKS 기본 VPC CNI는 NetworkPolicy 기능을 켜야 정책을 강제한다. 출발지에서 FAIL이 나와도 목적지가 PASS면 게이트는 PASS(개선)다.

### 최소 권한 — IAM-01 · ISO-01

| 시험 ID | 질문 | 출발지 | 목적지 | 합격 | 허용 대조군 |
|---|---|---|---|---|---|
| IAM-01.1 | 인증 없이 첨부 저장소 객체를 읽을 수 있는가? | 익명 `GET` (S3) | 익명 `GET` (SeaweedFS S3 API) | 거부 (403) | 앱 자격증명으로 같은 객체 읽기 성공 |
| IAM-01.2 | 앱 권한이 기준선보다 넓은가? | 아래 매핑표의 점검 항목 (Prowler · kube-bench) | 아래 매핑표의 점검 항목 (kube-bench) | 매핑 항목 위반 0 | — |
| ISO-01.1 | 다른 앱 · 다른 역할로 이 앱의 첨부 버킷 · DB에 접근할 수 있는가? | 다른 앱 SA로 첨부 버킷 읽기, 다른 앱 DB 역할로 앱 DB 접속 | 같음 | 모두 거부 | 앱 SA · 앱 DB 역할로는 성공 |

ISO-01.1은 Secret을 다루지 않는다(SEC-01.1과 중복).

### 백업 · 복구 — DR-01

| 시험 ID | 질문 | 출발지 | 목적지 | 합격 |
|---|---|---|---|---|
| DR-01.1 | 백업을 새 환경으로 실제 복원할 수 있는가? | 백업 직전에 표식 메시지를 쓰고 그 id와 `id ≤ 표식` 행 수를 기록 → CNPG 백업 → 빈 시험 클러스터에 복원 | 같음 | 복원 성공, 복원본에 표식 행이 있고 `id ≤ 표식` 행 수가 기록과 같다 |

쓰기가 계속 들어오는 중이라 "백업 시점의 행 수"는 정의되지 않는다. 표식 행 id를 기준으로 비교한다(demo-chat은 커밋 순서 = id 순서라 이 비교가 성립한다).

## 3. 통제 ID 매핑 (IAM-01.2)

Prowler와 kube-bench는 점검 수가 공급자 · 버전마다 다르므로 점검 개수를 비교하지 않는다(설계 문서 8.5). 아래 표에 넣은 점검 항목만 IAM-01.2로 판정한다.

| 통제 ID | 도구 | 범위 | 점검 ID | 상태 |
|---|---|---|---|---|
| IAM-01 | kube-bench (양쪽) | CIS Kubernetes Benchmark 5.1절 (RBAC와 서비스 계정) 중 앱 네임스페이스에 해당하는 항목 | T14에서 도구 출력 확인 후 확정 | 미정 |
| IAM-01 | Prowler (출발지) | 앱 IAM 역할 · 정책에 해당하는 IAM 점검 | T14에서 도구 출력 확인 후 확정 | 미정 |

점검 ID는 실제 도구 출력과 버전을 보고 채운다. 추측으로 채우지 않는다.

## 4. 판정

### 하위 시험 판정

| 판정 | 조건 |
|---|---|
| PASS | 시험이 정상으로 돌았고 관찰(`observed`)이 기대(`expected`)와 같고, 허용 대조군이 있으면 대조군도 통과 |
| FAIL | 시험이 정상으로 돌았고(대조군 통과) 관찰이 기대와 다르다 (예: 거부를 기대했는데 허용됨) |
| UNKNOWN | 아래 UNKNOWN 조건 중 하나 |

허용 대조군이 실패했으면 관찰 결과와 상관없이 UNKNOWN이다(FAIL도 PASS도 아니다).

### UNKNOWN 조건

**거부(PASS의 증거)와 관찰 불가(UNKNOWN)를 구분한다.** 접근 시도가 403으로 막히면 거부다. 결과를 보는 쪽(로그 검색, 상태 조회)이 403이면 관찰 불가다.

시험 결과(`TestResult`, T14)에 붙이는 사유는 6개다.

| 사유 코드 | 조건 |
|---|---|
| `POSITIVE_CONTROL_FAILED` | 허용 대조군이 실패했다 (서비스 장애, 시험 주체 준비 실패) |
| `OBSERVATION_DENIED` | 결과를 조회할 권한이 없거나 조회할 수단(수집기)이 없다 |
| `UNEXPECTED_RESPONSE` | 거부로 정해 두지 않은 응답 (500, 연결 끊김 등) |
| `TIMEOUT` | 시험이 제한 시간 안에 끝나지 않았다 |
| `TOOL_ERROR` | 시험 도구가 실행되지 않았거나 출력을 해석할 수 없다 |
| `NOT_RUN` | 시험이 실행되지 않았다 |

게이트(`GateRow.reason`, T19)가 비교할 때 판단하는 사유는 2개다.

| 사유 코드 | 조건 |
|---|---|
| `EVIDENCE_EXPIRED` | 한쪽이라도 증거 유효기간이 지났다 |
| `METHOD_MISMATCH` | 양쪽 `method_version`이 다르다 |

### 게이트 비교 (시험 ID 단위)

| 출발지 | 목적지 | 게이트 | 비고 |
|---|---|---|---|
| PASS | PASS | PASS | |
| FAIL | PASS | PASS | 개선 (`improved: true`) |
| 무관 | FAIL | BLOCK | 출발지도 FAIL이면 "동등"이 아니라 실패로 센다 |
| UNKNOWN | 무관 | BLOCK | |
| 무관 | UNKNOWN | BLOCK | |
| 증거 만료 · 방법 불일치 | | BLOCK | 판정보다 먼저 본다 |
| 예외 승인 | | EXCEPTION | 게이트에서 사람이 승인한다. 승인자 · 기간 · 대체 통제가 있어야 한다. 동등 판정에 넣지 않고 따로 표시한다 |

하나라도 BLOCK이면 이전 후보 등록과 전환을 막는다. 예외 승인 없는 FAIL · UNKNOWN이 0개여야 통과다(설계 문서 S1 · S3 인수 조건).

### 스키마가 강제하지 못하는 것 (T19가 코드로 검사)

- PASS면 `observed`와 `expected`가 같다.
- 행별 `decision`이 위 비교 규칙과 맞고, 전체 `decision`은 행 중 하나라도 BLOCK이면 BLOCK이다.
- 증거의 `expires_at`이 `collected_at`보다 뒤이고, 게이트 판정 시각에 만료되지 않았다.

## 5. 게이트 질문

원본 대조 게이트(T19)가 묻는 보안 질문이다. 데이터 경로 준비 질문(스키마 해시, 복제 지연 · 시퀀스 보정)은 T13 · T19가 정한다. 이 질문을 같은 `GateComparison`에 넣으려면 시험 ID 형식을 넓히는 스키마 변경이 필요하다.

| 순서 | 질문 | 시험 ID | 데모 기대 판정 |
|---|---|---|---|
| 데모 1 | 인증 없이 첨부 저장소 객체를 읽을 수 있는가? | IAM-01.1 | 출발지 거부 · 목적지 허용 → **BLOCK** (S2에서 AI가 고친 뒤 PASS) |
| 데모 2 | 다른 서비스 계정으로 채팅 DB Secret을 읽을 수 있는가? | SEC-01.1 | 양쪽 거부 → **PASS** |
| 데모 3 | 관리자 로그인 이벤트가 감사 로그에서 검색되는가? | AUD-01.1 | 출발지 검색됨 · 목적지 확인 불가 → **UNKNOWN → BLOCK** |
| 여분 1 | 암호화 없이 DB에 연결되는가? | ENC-01.3 | |
| 여분 2 | 외부에서 DB 포트에 붙을 수 있는가? | NET-01.2 | |
| 여분 3 | 다른 네임스페이스에서 DB에 붙을 수 있는가? | NET-01.3 | |

## 6. 결과 · 증거 형식

[`control-result.schema.json`](control-result.schema.json)이 계약이다. 세 가지 문서를 정의한다.

| 문서 | 만드는 쪽 → 쓰는 쪽 | 내용 |
|---|---|---|
| `ControlRun` | T14 → T19, T21 | 한 환경에서 한 번 돌린 시험 결과 묶음 (`results[]`) |
| `TestResult` | (`ControlRun` 안) | 시험 하나의 기대 · 관찰, 허용 대조군, 판정, UNKNOWN 사유, 증거 |
| `GateComparison` | T19 → T21 | 출발지 · 목적지 결과를 시험 ID별로 비교한 게이트 판정과 예외 승인 |

예외 승인은 시험기가 아니라 게이트에서 사람이 하므로 `GateRow.exception`에 둔다. API `GET /v1/environments/{id}/controls`(설계 문서 8.9)는 가장 최근 `ControlRun`을 돌려준다. 계약을 바꿀 때는 `schema_version`을 올리고 이 문서와 예시를 함께 고친다.

## 7. 다른 작업에 걸린 전제

| 시험 | 필요한 것 | 지금 담당 |
|---|---|---|
| AUD-01 출발지 | pgaudit, `log_connections`, EKS 컨트롤 플레인 `audit` 로그 | T6에 "pgaudit, CloudTrail 활성화"만 있음. `log_connections` · EKS audit 로그 추가 필요 |
| AUD-01 목적지 | pgaudit, `log_connections`, RKE2 audit 로그, 로그 수집 | `tasks.md`에 없음 |
| DR-01 양쪽 | CNPG 백업 설정과 복원용 빈 클러스터 | `tasks.md`에 없음 |

6개 통제 시험 결과(`ControlRun`)는 보안 탐지 통합(T33, `docs/security/detection-integration.md`)의 한 화면에도 Compliance Finding으로 들어간다. 이 계약을 바꾸면 T33 매핑 표도 함께 고친다.

## 8. 설계 문서 8.5와 다른 점

설계 문서(`design.html`, v0.1) 8.5는 고치지 않고, 시험을 구체화하며 달라진 점을 여기 모은다. 설계 문서를 다음 버전으로 올릴 때 이 표를 반영한다. **구현은 이 문서를 따른다.**

| 통제 | 설계 문서 8.5 | 이 문서 | 이유 |
|---|---|---|---|
| AUD-01 | 도구: "pgaudit, RKE2 · **CloudTrail** 검색" | 로그인: PostgreSQL `log_connections` / DDL: pgaudit / 권한 변경: **EKS 컨트롤 플레인 `audit` 로그(CloudWatch Logs)** · RKE2 audit 로그 | CloudTrail은 AWS API 호출만 기록하고 K8s 객체 변경(RoleBinding 등)은 남기지 않는다. pgaudit는 접속(로그인)을 기록하지 않는다 |
| ENC-01 | "앱–pooler–DB … CA · hostname 검증" (한 시험) | 앱 → pooler(ENC-01.1)와 pooler → DB(**ENC-01.5**)를 나눠 시험 | `psql`은 pooler까지만 연결해서 pooler → DB 구간이 평문이어도 통과한다 |
| SEC-01 | "회전 1회" | 회전 후 기존 연결을 끊고 새 연결로 `/api/health/db` · 쓰기 확인 (SEC-01.4) | 기존 연결은 비밀번호가 바뀌어도 유지된다. demo-chat의 `/readyz`는 DB를 보지 않는다(demo-chat#5) |
| DR-01 | "새 환경으로 실제 복원 성공" | 표식 행 id 기준으로 행 수 비교 (DR-01.1) | 쓰기가 계속 들어와 "백업 시점의 행 수"가 정의되지 않는다 |
| 판정 | "UNKNOWN은 차단" | UNKNOWN 사유 코드 8개, 허용 대조군, "관찰 못 함"(UNKNOWN)과 "관찰했더니 없음"(FAIL) 구분 | 서비스 장애로 인한 타임아웃을 "거부"로 오판하지 않기 위해 |

`tasks.md`의 T6에는 "pgaudit, CloudTrail 활성화"만 있어, `log_connections`와 EKS 컨트롤 플레인 `audit` 로그 활성화를 T6 담당에게 요청한다(7절).
