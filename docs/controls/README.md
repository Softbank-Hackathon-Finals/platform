# 6개 통제 시험 설계 (T5)

설계 문서 8.5의 6개 통제를 출발지(AWS EKS)와 목적지(온프렘 RKE2)에서 **같은 의미로** 시험하는 방법, 합격 기준, UNKNOWN 조건, 게이트 질문, 결과 · 증거 형식을 고정한다. 통제 시험기(T14)가 이대로 시험하고, 원본 대조 게이트(T19)와 콘솔(T21)이 결과를 읽는다.

| 파일 | 내용 |
|---|---|
| 이 문서 | 하위 시험 · 통제 ID 매핑 · 판정 규칙 · UNKNOWN 조건 · 게이트 질문 |
| [`control-result.schema.json`](control-result.schema.json) | 결과 · 증거 형식 계약 (JSON Schema 2020-12) |
| [`profiles.json`](profiles.json) | 평가 프로필: 시험별 불변조건과 게이트가 요구하는 시험 범위 (4절) |
| [`contract-cases.json`](contract-cases.json) | 계약 시험: T19 · T10 · T21이 거부해야 하는 입력과 문서 (4절) |
| [`examples/`](examples/) | 데모 게이트 질문 3개의 예시 데이터. 고치기 전(BLOCK)과 고친 뒤(PASS). T19 · T21 모의 데이터로 쓴다 |

## 1. 공통 원칙

- **같은 의미로 시험한다.** 도구가 달라도(Security Group ↔ NetworkPolicy, Secrets Manager ↔ OpenBao) 같은 질문에 같은 방식으로 답하게 맞춘다. 시험 방법은 `method_version`으로 관리하고, 양쪽 버전이 다르면 비교하지 않는다(UNKNOWN).
- **거부 시험에는 허용 대조군을 붙인다.** "다른 주체는 거부된다"를 보기 전에 "정상 주체는 된다"를 먼저 확인한다. 대조군이 실패하면 서비스 장애나 시험 준비 실패이므로 거부를 PASS로 세지 않는다(UNKNOWN).
- **"거부"는 정해진 응답으로만 인정한다.** 시험마다 거부로 볼 응답(예: `AccessDenied`, HTTP 403, `permission denied`)을 정해 두고, 그 외의 응답(500, 타임아웃, 연결 끊김)은 UNKNOWN이다. 예외는 네트워크 차단(NET-01.2 · NET-01.3)이다. 정책이 패킷을 버려 거부가 연결 제한시간 초과로 보이므로, 같은 시험 창의 대조군으로 "장애가 아님"을 증명한 경우에만 거부로 인정한다(2절 NET-01).
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

**차단(정책 드롭)과 장애(타임아웃)를 구분하는 증거.** 보안 그룹 · NetworkPolicy는 패킷을 버리므로 차단된 연결은 RST(연결 거부)가 아니라 제한시간 초과로 끝난다. 이것만으로는 DB나 시험 파드의 장애와 구분되지 않는다. 그래서 아래를 모두 만족할 때만 `observed: deny`다.

| | NET-01.2 (외부 → DB) | NET-01.3 (다른 NS → DB) |
|---|---|---|
| 시도 결과 | `refused`(RST) 또는 `timeout`(연결 제한시간 3초, 제안값 · 실측 후 조정) | 같음 |
| 대조군 ① 대상이 살아 있음 | 클러스터 안 앱에서 같은 DB 주소 · 포트 연결 성공 | 앱 NS 시험 파드에서 같은 DB 주소 · 포트 연결 성공 |
| 대조군 ② 시도한 쪽이 살아 있음 | 같은 외부 호스트에서 공개 진입점(Tunnel) 연결 성공 | 같은 시험 파드에서 클러스터 DNS 조회와 허용된 시험용 서비스 연결 성공 |
| 시간 | 시도와 두 대조군이 같은 시험 창(앞뒤 60초 안, 제안값) | 같음 |

- 시도가 `connected`면 `observed: allow`(FAIL)다.
- 대조군 ① · ② 중 하나라도 실패하면 관찰과 상관없이 UNKNOWN(`POSITIVE_CONTROL_FAILED`)이다. 제한시간 초과를 거부로 세는 근거가 사라지기 때문이다.
- 증거(`excerpt`)에 시도와 대조군마다 결과 종류 · 걸린 시간 · 시각을 남긴다. 예: `probe=timeout(3000ms) ctl_target=connected(14ms) ctl_probe_dns=ok ctl_probe_svc=connected(9ms)`.
- 차단을 만든 설정(보안 그룹 규칙, `NetworkPolicy` · `SecurityGroupPolicy`)의 덤프 해시를 증거로 함께 남긴다. 판정은 동작으로 하고, 설정 덤프는 원인 기록이다(설정 이름이 같다고 PASS로 보지 않는다).
- UNKNOWN 사유 `TIMEOUT`은 시험 전체가 제한 시간 안에 끝나지 않은 경우다. 위의 연결 제한시간 초과와 다르다.

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

게이트(`GateRow.reason`, T19)가 판단하는 사유는 6개다. 아래 "게이트 검사 순서"의 앞 단계에서 걸리면 뒤 단계는 보지 않는다.

| 사유 코드 | 조건 |
|---|---|
| `MISSING_RESULT` | 프로필이 요구하는 시험 결과가 한쪽이라도 없다 |
| `DUPLICATE_RESULT` | 한 실행 묶음에 같은 시험 ID 결과가 두 건 이상이다 |
| `INVARIANT_VIOLATION` | 결과가 프로필의 시험별 불변조건(통제 ID, 기대값, 대조군 필수 여부)과 다르거나, 판정이 관찰과 맞지 않는다(PASS인데 관찰 ≠ 기대, FAIL인데 관찰 = 기대) |
| `METHOD_MISMATCH` | 한쪽이라도 `method_version`이 프로필과 다르다 (그래서 양쪽이 달라도 여기서 걸린다) |
| `EVIDENCE_EXPIRED` | 한쪽이라도 증거 유효기간이 지났다 |
| `EXCEPTION_INVALID` | 이 행에 해당하는 예외 승인 기록이 있지만 평가 시각에 유효하지 않다 |

### 평가 프로필

게이트가 어떤 시험을 요구하는지는 실행 결과가 아니라 **버전 있는 평가 프로필**([`profiles.json`](profiles.json))이 정한다. 게이트 행은 프로필의 `required_test_ids`에서 만든다. 그래서 결과가 빠진 시험도 행으로 남아 BLOCK이 된다(빠진 시험이 판정에서 사라지지 않는다). 프로필 밖 시험 결과는 게이트 판정에 쓰지 않는다.

| 프로필 | 시험 | 쓰는 곳 |
|---|---|---|
| `demo-gate` v1 | IAM-01.1, SEC-01.1, AUD-01.1 (5절 데모 질문) | 데모 게이트, `examples/` |
| `full` v1 | 하위 시험 20개 전체 | 설계 문서 S1 · S3 인수 조건(6개 통제) |

`tests`는 시험별 불변조건이다: 통제 ID, 시험 방법 버전, 기대값(2절 표의 "합격"), 허용 대조군 필수 여부(2절 표의 "허용 대조군"). 프로필이나 불변조건을 바꾸면 버전을 올린다.

### 게이트 검사 순서 (T19)

1. **실행 묶음**: 양쪽 `ControlRun`의 `app`이 프로필의 `app`과 같다(`APP_MISMATCH`), 출발지는 `role: source` · 목적지는 `role: target`이다(`ENVIRONMENT_ROLE_MISMATCH`), 두 환경 ID가 다르다(`SAME_ENVIRONMENT`). 하나라도 어긋나면 `run_issues`에 넣고 게이트 전체를 BLOCK한다.
2. **시험마다** (`required_test_ids` 순서대로 한 행씩): 결과가 양쪽에 정확히 한 건 → 불변조건 → 방법 버전 → 증거 시각(`collected_at < expires_at`, 평가 시각 `< expires_at`) → 아래 비교.
3. **예외 승인**: 비교 결과가 `TARGET_FAIL`인 행만 예외 대상이다. 관찰하지 못한 것(UNKNOWN)과 위 1 · 2단계의 무결성 문제는 예외로 덮지 않는다.
4. **전체 판정**: `run_issues`가 있거나 BLOCK 행이 하나라도 있으면 BLOCK. 이것은 스키마도 강제한다.

| 출발지 | 목적지 | 게이트 | 사유 |
|---|---|---|---|
| UNKNOWN | 무관 | BLOCK | `SOURCE_UNKNOWN` |
| PASS · FAIL | UNKNOWN | BLOCK | `TARGET_UNKNOWN` |
| PASS · FAIL | FAIL | BLOCK | `TARGET_FAIL`. 출발지도 FAIL이면 "동등"이 아니라 실패로 센다 |
| FAIL | PASS | PASS | `IMPROVED` (`improved: true`) |
| PASS | PASS | PASS | `BOTH_PASS` |
| | FAIL + 유효한 예외 승인 | EXCEPTION | `EXCEPTION_APPROVED`. 동등 판정에 넣지 않고 따로 표시한다 |

하나라도 BLOCK이면 이전 후보 등록과 전환을 막는다. 예외 승인 없는 FAIL · UNKNOWN이 0개여야 통과다(설계 문서 S1 · S3 인수 조건).

### 예외 승인의 유효성

예외 승인의 근거는 **T10이 저장한 서버 승인 기록**이다. JSON의 `approved_by` 문자열은 표시용이고 권한 근거가 아니다. 승인자 권한 확인은 기록을 만들 때 T10이 한다.

- `approval_id`로 서버 기록을 찾고, 문서의 예외가 그 기록과 같아야 한다.
- 기록의 `scope`(앱, 목적지 환경 ID, 시험 ID, 프로필 ID · 버전, 평가 결속값 `context_digest`)가 지금 게이트와 같아야 한다. 다르면 이 행의 예외가 아니다(T19는 적용하지 않고, T10 · T21은 문서를 거부한다).
- `approved_at ≤ evaluated_at < expires_at`이어야 한다. 아니면 `EXCEPTION_INVALID`로 BLOCK.

#### 계획·평가 결속 (T10 · T19)

여기서 예외는 **한 계획의 고정된 평가에 대한 예외**다. 유효기간 동안 여러 계획에 적용하는 환경 정책 예외가 아니며, 운영 실행 승인도 대신하지 않는다.

T10은 인증된 tenant 안에서 아래 평가 context를 불변으로 저장한다. `gate_id`는 이 context 하나를 식별한다. 계획 수정, 재측정, 원본 교체, 환경·프로필 변경에는 새 context와 새 `gate_id`를 발급하고 예외를 다시 승인받는다. 같은 입력을 다시 조회하거나 검증 시각만 갱신하는 것은 재측정이 아니다.

| context 필드 | 서버가 고정할 내용 |
|---|---|
| `tenant_id`, `app`, `gate_id` | 소유 조직, 앱, 고정 평가 ID |
| `plan_digest` | 승인 대상 불변 계획의 내용 해시 |
| `profile_id`, `profile_version`, `catalog_digest` | 프로필 ID·버전 및 시험 불변조건을 포함한 전체 카탈로그 해시 |
| `source_environment_id`, `source_environment_ref`, `target_environment_id`, `target_environment_ref` | 등록 환경 ID 및 실제 자원 참조 |
| `source_ref`, `target_ref`, `source_digest`, `target_digest` | 양쪽 불변 시험 결과 참조 및 문서 내용 해시 |

`context_digest`는 위 **15개 필드 전체**를 JSON 객체로 직렬화한 UTF-8 바이트의 SHA-256 소문자 64자리다. 문자열은 Unicode를 그대로 유지하고, 키는 정렬하고 구분 공백은 제거한다. 모든 context 값은 문자열이다. Python 기준은 `json.dumps(context, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')`다. 원본 결과·카탈로그 해시도 같은 직렬화를 쓴다. RFC 8785/JCS와 같다고 가정하지 않는다. 다른 언어 구현은 [`evaluation-contexts.json`](evaluation-contexts.json)과 게이트 예시의 결속값을 상호운용 시험으로 확인한다. 증거의 `output_sha256`은 이와 달리 원본 출력 바이트의 해시다.

- `GateComparison.context_digest`, `GateRow.exception.scope.context_digest`, 서버 승인 기록의 결속값이 **현재 서버 context의 해시와 모두 같아야 한다**.
- T10은 승인 생성 시 현재 context와 승인자 권한을 확인하고 승인 ID·revision·폐기 상태를 저장한다. T19는 현재 서버 기록을 읽기만 한다. 문서의 값을 서버 승인 기록으로 복사해 결속을 재발급하지 않는다.
- 소비자는 서버 context의 `gate_id`와 문서의 `gate_id`, 양쪽 결과·환경·프로필도 대조한다. 결속값 문자열만 복사했거나, 결속값까지 새로 계산했어도 기존 서버 승인이면 거부한다.
- 예외가 없는 PASS 문서도 현재 context와 다르면 거부한다. 계획 변경 후 예전 PASS를 현재 계획의 승인 근거로 재사용하지 않는다.
- 최초 평가 시각뿐 아니라 **실제 소비 시각에도** 기간·폐기·현재 권한을 검사한다. 조회 실패·출처 불명·등록 변경은 BLOCK/거부로 처리한다.

T10의 저장소가 gate ID 재할당을 막는 책임을 갖는다. T19도 context digest를 다시 대조하여 같은 ID의 계획·원본이 변경된 경우 과거 문서와 예외를 거부한다. 이 계약은 T10 저장소가 이미 연결됐다는 의미가 아니며, 실제 연결은 T10/T19 통합 시험에서 확인한다.

### 게이트 문서를 쓰는 쪽의 검사 (T10 · T21)

`GateComparison` 문서를 승인 근거로 쓰기 전에 문서를 그대로 믿지 않고 확인한다. 하나라도 어긋나면 그 문서로 승인하지 않는다.

- 스키마 검증 (전체 판정과 행 판정의 일관성, 예외의 `approval_id` · `scope` 필수는 스키마가 강제한다)
- 행의 시험 ID가 프로필의 `required_test_ids`와 정확히 같다 (빠짐 · 중복 없음)
- `EXCEPTION` 행마다 위 "예외 승인의 유효성"
- 서버의 현재 계획·고정 평가 context와 문서의 `gate_id` · `context_digest` · 결과 참조를 대조하고, 예외가 없어도 다른 계획·평가 문서는 거부

### 계약 시험

[`contract-cases.json`](contract-cases.json)이 계약 시험이다. 기준 예시에 RFC 6902 JSON Patch를 적용해 만든 입력과 기대 결과를 담는다. T19와 T10 · T21 구현은 이 사례를 모두 통과해야 한다.

| 묶음 | 사례 |
|---|---|
| `gate_input` (T19) | 결과 비어 있음, 같은 시험 두 건, 대조군 생략, 통제 ID 바꿈, 기대값 바꿈, 방법 버전 다름, 앱 · 환경 역할 · 같은 환경, 프로필 밖 결과, 만료 · 대상 불일치 · 평가 뒤 승인된 예외, 유효한 예외, 대조군 실패인데 PASS, 판정과 관찰 불일치 2종 |
| `gate_document` (T10 · T21) | 프로필 3개 중 1개 행만 남긴 PASS, 중복 행, 판정 불일치 2종, 만료된 예외로 PASS, 서버 기록 없는 예외, 서버 기록과 다른 예외, `approval_id` 없는 예외, 실행 묶음 문제가 있는데 PASS |
| `evaluation_binding` (T10 · T19) | 같은 고정 평가의 재조회 허용, 문서 gate ID 변경, 다른 계획·게이트, 재측정, 같은 run ID의 증거 변경, 문서와 예외 결속값 위조 거부 (7건) |

### 스키마가 강제하지 못하는 것

위 검사 중 스키마가 강제하는 것(시험 ID와 통제 ID 일치, 대조군이 실패했는데 PASS · FAIL, 전체 판정과 행 판정의 일관성, 예외의 필수 필드) 외에는 코드가 검사한다. 특히 아래는 스키마로 표현할 수 없다.

- PASS면 `observed`와 `expected`가 같다.
- 프로필 대조: 필수 시험 · 정확히 한 건 · 불변조건 · 방법 버전
- 시각 비교: 증거 만료, 예외 승인 기간
- 다른 문서와의 대조: 두 `ControlRun`의 앱 · 환경, 불변 계획·평가 context, 현재 서버 승인 기록과 폐기 상태

## 5. 게이트 질문

원본 대조 게이트(T19)가 묻는 보안 질문이다. 데이터 경로 준비 질문(스키마 해시, 복제 지연, PK, 시퀀스 보정 리허설)은 T19가 정한다(`tasks.md` T19). 시퀀스 보정 리허설에는 T13의 시퀀스 비교를 쓸 수 있다. 행 수 · 내용 해시 같은 데이터 동일성은 게이트가 아니라 컷오버 최종 비교(T12 · T13)가 판정한다(설계 문서 8.5). 데이터 경로 질문을 같은 `GateComparison`에 넣으려면 시험 ID 형식을 넓히는 스키마 변경이 필요하다.

데모 게이트는 `demo-gate` 프로필(데모 질문 3개)로 판정한다. 여분 질문을 데모에 넣으려면 프로필 버전을 올려 추가한다.

| 순서 | 질문 | 시험 ID | 데모 기대 판정 |
|---|---|---|---|
| 데모 1 | 인증 없이 첨부 저장소 객체를 읽을 수 있는가? | IAM-01.1 | 출발지 거부 · 목적지 허용 → **BLOCK** (S2에서 AI가 고친 뒤 PASS) |
| 데모 2 | 다른 서비스 계정으로 채팅 DB Secret을 읽을 수 있는가? | SEC-01.1 | 양쪽 거부 → **PASS** |
| 데모 3 | 관리자 로그인 이벤트가 감사 로그에서 검색되는가? | AUD-01.1 | 출발지 검색됨 · 목적지 확인 불가 → **UNKNOWN → BLOCK** |
| 여분 1 | 암호화 없이 DB에 연결되는가? | ENC-01.3 | |
| 여분 2 | 외부에서 DB 포트에 붙을 수 있는가? | NET-01.2 | |
| 여분 3 | 다른 네임스페이스에서 DB에 붙을 수 있는가? | NET-01.3 | |

## 6. 결과 · 증거 형식

[`control-result.schema.json`](control-result.schema.json)이 계약이다. 다음 문서를 정의한다.

| 문서 | 만드는 쪽 → 쓰는 쪽 | 내용 |
|---|---|---|
| `ControlRun` | T14 → T19, T21 | 한 환경에서 한 번 돌린 시험 결과 묶음 (`results[]`) |
| `TestResult` | (`ControlRun` 안) | 시험 하나의 기대 · 관찰, 허용 대조군, 판정, UNKNOWN 사유, 증거 |
| `GateComparison` | T19 → T10, T21 | 평가 프로필 기준으로 출발지 · 목적지 결과를 시험 ID별로 비교한 게이트 판정과 예외 승인 |
| `EvaluationProfiles` | T5 → T19, T10 | 시험별 불변조건과 게이트가 요구하는 시험 범위 (`profiles.json`) |

예외 승인은 시험기가 아니라 게이트에서 사람이 하므로 `GateRow.exception`에 둔다. 권한 근거는 `approval_id`가 가리키는 T10 서버 승인 기록이다(4절). `evaluation-contexts.json`은 문서 hash를 재현하기 위한 비밀값 없는 시험 벡터이며 서버 등록을 대체하지 않는다. 이번 보완은 미병합 v1 계약의 필수 필드 추가다. 이전 초안의 결속값 없는 게이트·예외는 자동 보충하지 않고 재평가·재승인한다. API `GET /v1/environments/{id}/controls`(설계 문서 8.9)는 가장 최근 `ControlRun`을 돌려준다. 계약을 바꿀 때는 `schema_version`을 올리고 이 문서와 예시를 함께 고친다.

## 7. 다른 작업에 걸린 전제

| 시험 | 필요한 것 | 지금 담당 |
|---|---|---|
| AUD-01 출발지 | pgaudit, `log_connections`, EKS 컨트롤 플레인 `audit` 로그 활성화와 로그 위치 | 활성화는 **T6**(류지환, PR #51 리뷰에서 반영하기로 함), 수집 · 검색 검증은 **T34**(이상무) |
| AUD-01 목적지 | pgaudit, `log_connections`, RKE2 audit 로그, 로그 수집 | **T34**(이상무) |
| DR-01 양쪽 | CNPG 백업 설정과 복원용 빈 클러스터 | **T35**(이상무, P0) |

`full` 프로필은 DR-01.1을 요구하고, 설계 문서 S1 · S3 인수 조건은 6개 통제 전체에 "예외 승인 외 FAIL · UNKNOWN 0개"를 요구한다. T35가 끝나기 전에는 DR-01.1이 `NOT_RUN` → UNKNOWN이고 `full` 게이트는 BLOCK이다(UNKNOWN은 예외로 덮지 않는다). 그래서 T35를 P1에서 P0로 올렸다(PR #51). 데모 게이트(`demo-gate`)는 DR-01을 쓰지 않는다.

6개 통제 시험 결과(`ControlRun`)는 보안 탐지 통합(T33, `docs/security/detection-integration.md`)의 한 화면에도 Compliance Finding으로 들어간다. 이 계약을 바꾸면 T33 매핑 표도 함께 고친다.

## 8. 설계 문서 8.5와 다른 점

설계 문서(`design.html`, v0.1) 8.5는 고치지 않고, 시험을 구체화하며 달라진 점을 여기 모은다. 설계 문서를 다음 버전으로 올릴 때 이 표를 반영한다. **구현은 이 문서를 따른다.**

| 통제 | 설계 문서 8.5 | 이 문서 | 이유 |
|---|---|---|---|
| AUD-01 | 도구: "pgaudit, RKE2 · **CloudTrail** 검색" | 로그인: PostgreSQL `log_connections` / DDL: pgaudit / 권한 변경: **EKS 컨트롤 플레인 `audit` 로그(CloudWatch Logs)** · RKE2 audit 로그 | CloudTrail은 AWS API 호출만 기록하고 K8s 객체 변경(RoleBinding 등)은 남기지 않는다. pgaudit는 접속(로그인)을 기록하지 않는다 |
| ENC-01 | "앱–pooler–DB … CA · hostname 검증" (한 시험) | 앱 → pooler(ENC-01.1)와 pooler → DB(**ENC-01.5**)를 나눠 시험 | `psql`은 pooler까지만 연결해서 pooler → DB 구간이 평문이어도 통과한다 |
| SEC-01 | "회전 1회" | 회전 후 기존 연결을 끊고 새 연결로 `/api/health/db` · 쓰기 확인 (SEC-01.4) | 기존 연결은 비밀번호가 바뀌어도 유지된다. demo-chat의 `/readyz`는 DB를 보지 않는다(demo-chat#5) |
| DR-01 | "새 환경으로 실제 복원 성공" | 표식 행 id 기준으로 행 수 비교 (DR-01.1) | 쓰기가 계속 들어와 "백업 시점의 행 수"가 정의되지 않는다 |
| 판정 | "UNKNOWN은 차단" | 시험 UNKNOWN 사유 6개 · 게이트 사유 6개, 허용 대조군, "관찰 못 함"(UNKNOWN)과 "관찰했더니 없음"(FAIL) 구분, 네트워크 차단은 대조군 2개로 장애와 구분 | 서비스 장애로 인한 타임아웃을 "거부"로 오판하지 않기 위해 |
| 게이트 범위 | (없음) | 버전 있는 평가 프로필이 필수 시험과 시험별 불변조건을 정한다 | 결과가 빠진 시험이 판정에서 사라지지 않게 하고, 대조군 생략 · 기대값 변경을 막기 위해 |
| 예외 승인 | "승인 · 기간 · 대체 통제" | 서버 승인 기록(`approval_id`)과 계획·평가 결속값(`context_digest`)에 결속, 평가·소비 시각에 유효해야 함, FAIL에만 적용 | 문서 안의 승인자 문자열은 권한 근거가 될 수 없다 |

`tasks.md`의 T6에는 "pgaudit, CloudTrail 활성화"만 있다. `log_connections`와 EKS 컨트롤 플레인 `audit` 로그 활성화와 로그 위치 전달은 T6 담당(류지환)이 반영하고, 수집 · 검색 검증은 T34가 한다(7절).
