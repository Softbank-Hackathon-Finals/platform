# T19 통제 게이트

병합된 T5의 프로필·시험 불변조건·서버 예외 승인 계약을 실행하는 결정적 Python 모듈이다. 보안 게이트 계산과 T10/T21의 소비 검증을 제공한다. LLM·클라우드 변경·시험 실행·예외 승인 발급은 하지 않는다.

**범위:** T19의 보안 전제조건. `CombinedGateService`는 T13 데이터 준비 관측과 보안 결과를 함께 검사한다. 선택적 T13 스키마 수집기를 포함하며 T14 환경 연결·T10 운영 공급자 연결은 별도다. 선택적 T10 어댑터는 실제 승인·실행 접수 코드와 SQLite/PostgreSQL에서 시험한다. `SecurityPrerequisite.authorizes_cutover`는 항상 `False`다. T19 이슈 전체의 완료나 실환경 동등성 검증 완료로 간주하지 않는다.

`PlanGateSession`은 최초 평가(`initial`, application ID 없음)와 반영 후 평가
(`post_apply`, 성공한 Application ID)를 구분한다. 반영 후 단계와 application ID는
세션 fingerprint에 포함되며 최초 fingerprint는 유지한다. `T10GateAuthority`가 현재 승인 결속의 같은 값과
대조한다. 최초 단계는 계획의 `profile_id/version`, 반영 후 단계는 별도로
고정된 `recheck_profile_id/version`을 사용한다. 두 필드가 모두 없는 기존 binding은
최초 단계에서만 허용한다. 부분 누락·반영 후 필드 누락·다른 단계·다른
Application의 승인을 재사용하면 실행 전에 거부한다. T10 호스트의
`GateRecords.cutover_authority` 조립은 저장된 결속을 자동 연결하며 현재
운영 권한·원본 인증 공급자를 명시적으로 요구한다.

## 계약 출처

`src/bravo_gate/contracts/`는 develop `626bca0`의 `docs/controls/` 계약을 고정한다. 스키마는 기존 T5 고정본과 바이트 단위로 같고, 최신 서버 정본 소비 사례도 함께 가져왔다. 프로필은 그 뒤 T31 반영 뒤 재대조용 `apply-recheck` v1을 더했고, 이에 따라 바뀐 카탈로그·context 해시를 계약 사례에 반영했다. 경로·커밋·해시는 `provenance.json`에 있다. 원본과 패키지 사본의 일치 여부를 시험한다. 계약 변경은 상호운용 시험과 해시 갱신을 함께 수행한다.

원본 결과의 `RunSnapshot.fingerprint`는 UTF-8 JSON을 key 정렬·구분 공백 제거·원문 Unicode 유지로 직렬화한 SHA-256이다. 임의 RFC 8785 구현과 같다고 가정하지 않으며 T10은 제공된 함수를 사용한다. 네트워크 증거의 `output_sha256`은 별도로 원본 바이트 전체를 해시한다.

## 판정 경계

| 검사 | 동작 |
| --- | --- |
| 필수 시험 | 서버가 선택한 버전 있는 프로필에서 행을 만든다. 빈 결과·누락·중복을 BLOCK으로 남긴다 |
| 시험 불변조건 | ID/control/expected, 대조군 필수 여부, PASS/FAIL과 실제 관찰 일치 검사 |
| 시각·방법 | 방법 버전, 실행 구간, 관측·만료, 미래/만료 근거 검사 |
| 실행 묶음 | 같은 앱, 출발/목적 역할, 다른 환경, 등록된 실제 환경 참조 검사 |
| 비교 | 출발 FAIL→목적 PASS만 개선. 목적 FAIL·어느 쪽 UNKNOWN은 BLOCK |
| 예외 | TARGET_FAIL에만 적용. 누락·중복·불변조건·UNKNOWN·만료를 덮지 못함 |
| 예외 권한 | JSON `approved_by`를 신뢰하지 않음. 인증된 서버 기록의 ID·revision·폐기·기간·대체 통제 확인 |
| 예외 결속 | 문서·예외 scope·서버 기록의 `context_digest`를 대조. tenant·계획·gate·profile hash·양쪽 원본 결과 hash에 결속 |
| 소비 | 제출된 PASS/EXCEPTION을 그대로 승인하지 않고 현재 등록·원본·승인으로 다시 계산 |
| 재검증 | 저장소/발행자 상태·신원·기한을 재조회. 검사 중 변경·기한 경과 시 전제조건 미발급 |

`demo-gate`는 3개, `full`은 20개, `apply-recheck`(T31 반영 뒤 재대조)는 3개 시험을 요구한다. `full`에서 DR-01.1이 없으면 항상 BLOCK이며 예외로 무시할 수 없다. 운영 전환에서 어느 프로필을 요구하는지는 T10의 서버 정책이 선택한다. UI가 작은 프로필로 바꿔 통과시키는 API는 제공하지 않는다.

## T10/T14 연결

`GateService`는 다음 서버 의존성을 **모두 명시적으로** 요구한다. 브라우저 입력을 그대로 반환하는 공급자나 항상 True인 인증 콜백은 운영 구현이 아니다.

| 공급자 | 책임 |
| --- | --- |
| `context_provider(actor, context_id)` | 인증된 tenant/앱과 계획·게이트·프로필·환경·양쪽 불변 결과 참조/hash를 읽음 |
| `run_provider(actor, context, side)` | T14의 불변 결과 `RunSnapshot`. 같은 ID의 내용 변경 거부 |
| `approval_provider(actor, context, approval_id)` | T10의 권한 검증을 거쳐 저장된 승인 revision과 폐기 상태를 읽음 |
| `authenticate_context/run/approval` | 각 발급 역할·원본·범위·현재 신뢰를 검증. 명시적 `True` 또는 정확한 범위·기한의 `VerifiedAuthority`만 허용. `None`, False, truthy 객체는 거부 |
| `report_provider(actor, ctx)` / `authenticate_report` | 서버에 별도로 저장한 불변 결과를 gate ID로 조회·인증. 제출 문서 전체와 정확히 대조. 없음·조회 실패·미인증·내용 변경은 거부 |
| `authorize` | 현재 사용자/서비스의 `gate:evaluate`, `gate:consume`, `gate:use-exception` 권한을 각각 검사. `approved_by` 표시값과 구분 |
| `clock` | 서버 UTC 시계. 요청 JSON에서 평가 시간을 고르지 않음 |

```python
report = service.evaluate(actor, context_id, approval_ids=("server-approval-id",))
# 신뢰된 호스트가 이 결과를 불변 저장한 뒤에만 소비할 수 있다.
# evaluate() 자체는 저장하지 않으며 요청 본문을 정본으로 등록하면 안 된다.
# T10은 저장한 report와 context를 다시 읽고 실제 실행 직전에 호출한다.
security = service.require_security(actor, context_id, serialized_report,
                                    approval_ids=("server-approval-id",))
# security는 최대 5초·원래 근거 기한 안의 보안 전제조건이다.
# 데이터 준비·운영 승인·현재 상태·실행권이 별도로 통과해야 컷오버 가능하다.
```

`Ed25519Authority`는 계획 context·문서 hash·발급 목적을 기존 서버 공개키 원장과 대조하는 구체 서명 검증기다. 키 폐기·교체·역할 불일치를 재검증한다. 평가기에 서명 개인 키를 제공하지 않는다. 입력 JSON의 `evidence.signature` 필드만으로 인증하지 않는다. 서명은 발급 출처를 확인하며, 실제 시험이 제대로 실행됐다는 사실은 T14 수집기 자격으로 검증해야 한다.

서명 검증기는 `VerifiedAuthority(context_digest, subject_digest, purpose, valid_until)`을 반환한다. `valid_until`은 키와 서명의 만료 중 빠른 시각이며 최종 전제조건도 이를 넘지 않는다. 목적은 `context`, `run-source`, `run-target`, `exception`이다. 권한 판정의 기한이 있으면 `subject_digest=permission_subject(actor, context_digest, action)`, `purpose=permission:<권한명>`으로 반환한다. 예를 들어 `permission:gate:consume`이다. `permission_subject`는 `kind=gate-permission.v1`, 인증된 actor, context digest, action을 정규화하여 해시하므로 다른 사용자의 권한 증명을 재사용할 수 없다. actor는 요청 본문이 아니라 인증 계층에서 가져온다. 네트워크 출처는 `network-binding`(등록 내용 hash), `network-artifact`(원본 바이트 hash)를 쓴다. 만료 있는 서명·세션·권한의 결과를 `True`로 변환해 기한을 버리는 어댑터는 이 계약에 맞지 않는다. `True`는 별도의 만료가 없는 서버 정책을 매번 직접 검사한 어댑터에만 사용한다. 예외를 던지지 않고 반환만 하는 기존 검증 함수는 명시적 반환 계약으로 바꿔야 한다.

예외 승인 생성은 T10에서 승인자 권한·분리 승인·감사 기록을 확인한 후 수행한다. T19는 그 기록을 읽기만 한다. 임의 JSON 승인 배열이나 사용자 문자열을 DB 승인으로 전환하지 않는다. 소비 문서의 예외는 서버 기록과 완전히 일치해야 한다. 승인 폐기 또는 계획/원본 변경 뒤 예전 PASS 문서를 재사용할 수 없다. 예외가 없는 PASS 문서도 `context_digest`를 대조한다. 계획·gate·재측정 결과가 바뀌면 새 context와 예외 재승인이 필요하다. 같은 고정 입력을 재조회하는 것은 허용한다. 결속값이 빠진 이전 초안을 자동 보충하지 않는다.

## 네트워크 드롭과 장애

NET-01.2/01.3의 PASS에는 `CollectedNetworkProofAuthority`가 필요하다. T14 집계 원문 한 개와 DB·풀러 개별 원문 두 개를 등록된 저장소에서 읽는다. URI를 임의로 가져오거나 `excerpt`를 정규식으로 해석하지 않는다.

- 서버의 `NetworkRunRegistration`이 계획 context, 양쪽 중 어느 환경인지, 앱, 실제 run ID, 시험·방법 버전, DB·풀러의 등록 hash·정책 hash와 기한을 고정한다. 두 경로의 등록은 서로 달라야 한다.
- 집계와 개별 원문 모두 SHA-256·서버 출처를 검증하고, 모든 경로가 같은 run·앱·환경·시험에 속하는지 확인한다. 검증 마지막에 등록·원문을 다시 읽고 재인증한다.
- 각 경로의 연결 거부 또는 3초 이상 timeout, 시험 전후 정상 연결·시험 주체의 정상 경로, NET-01.3의 전후 DNS 성공을 60초 창에서 함께 확인한다. 하나라도 누락·중복·변조·실패하면 전체 시험의 PASS 근거로 사용할 수 없다.
- `GateService`는 검증된 두 역할과 현재 로드한 원본 run ID까지 대조한다. 예전 단일 경로 `NetworkProofAuthority`는 진단용으로 남지만 전체 NET 시험의 통과 근거로는 사용할 수 없다. 기존 호스트는 두 경로 수집·등록·원문 조회를 연결해야 한다.
- 검증·등록·출처 권한의 만료 중 가장 빠른 시각을 보안 전제조건 기한에 반영한다.

형식 1은 기존 DB 5432·풀러 6432 계약이다. 형식 2는 인증된 등록에 있는 역할별 리스너와 코디네이터 요청/응답 시간 구간을 검증한다. 원격 시계 차이는 최대 1초, 응답은 최대 5초이며 전체 60초 제한은 유지한다. **형식 2를 파싱하는 것과 T5 평가 프로필에 방법 2를 승인하는 것은 별개다.** 이 변경은 기본 카탈로그·기존 계획·T10 등록을 바꾸지 않는다.

이것은 선정한 도달성 시험을 해석하는 계약이다. 대조군이 성공한 timeout도 정책 드롭의 직접 증명으로 표시하지 않는다. T14의 인증된 등록이나 원문이 없으면 미검증으로 BLOCK한다. 실제 호스트의 실행 주체·정책·Service/EndpointSlice/Pod UID 등록 확인은 호스트 책임이며, 클라이언트가 보낸 hash 문자열만으로 인증하지 않는다.

연결 계약과 검증 한계는 [T14 네트워크 원문 인수](docs/network-bundle.md)를 참고한다.

## 상태·저장·실행 책임

이 모듈은 새 승인 DB나 실행 조정기를 만들지 않는다. T10은 불변 계획·원본 결과·평가 context와 승인 revision/폐기를 서버 저장소에 보관하고, 등록 변경을 CAS/트랜잭션으로 관리해야 한다. 검증 예외(`GateError`)는 차단/미확인으로 처리하고 이전 PASS로 대체하지 않는다. 감사용 과거 판정은 현재 실행 허가가 아니다. 반환된 context/report digest와 근거 기한을 실제 실행 조정기에 연결해야 한다.

서비스 경계 전체를 분산 트랜잭션으로 만들었다고 주장하지 않는다. T10의 작업 상태·승인·실행 epoch 재검사, T12의 fencing·최종 비교가 계속 필요하다. 보안과 데이터 준비를 합쳐 최종 승인하는 책임은 T10/T19 접점에 있다.

### 운영 연결 전 필수 조건

- T10의 context 조회는 인증된 actor의 tenant 경계를 먼저 적용한다. gate ID는 tenant 내 불변 등록이며 현재 계획·프로필을 서버 정책에서 선택한다.
- 실제 실행 접수는 현재 승인 revision/폐기, 실행 epoch, 계획·context·원본 hash, 보안 보류, 데이터 준비를 동일한 트랜잭션/CAS 조건으로 확인하고 outbox에 넣는다. 이 모듈의 반복 조회만으로 원자성이나 즉시 폐기 적용을 주장하지 않는다.
- 실행기는 출고한 작업의 epoch·기한을 다시 검사하고 이전 실행기의 쓰기를 fencing한다. 만료·폐기·불명 상태에서 과거 PASS로 대체하지 않는다.
- `SecurityPrerequisite`는 프로세스 내부 검증 결과다. JSON으로 받은 같은 형태의 객체나 사용자가 만든 객체는 권한 증명이 아니다. `report_digest`는 실제 소비한 문서의 정규화 JSON hash이며 재계산 중 갱신한 시각의 문서 hash가 아니다.
- API는 인증·tenant별 요청 제한·본문 크기 제한을 적용하고, 저장소·키 조회·수집기 호출에 timeout을 둔다. 동기 콜백의 무한 대기를 이 라이브러리가 강제로 취소할 수는 없다. 운영 워커의 외부 timeout과 재시도 정책을 시험해야 한다.
- 승인자 권한·분리 승인·키 보관/회전·감사 기록의 실제 구현과 T14 수집기의 신뢰성은 통합 시험으로 검증한다. 신뢰된 서버 어댑터를 침해한 경우까지 이 Python 모듈만으로 방어하지 않는다.

외부 공급자가 예기치 않은 예외를 내면 공개 오류는 `AUTHORITY_UNAVAILABLE`로 제한하며 내부 SDK 오류 문자열을 노출하지 않는다. `GateError`는 어댑터가 비밀 없는 고정 오류 코드로만 생성해야 한다. 운영 API도 traceback이나 예외 cause를 응답에 포함하지 않는다.

## 검증

```sh
cd services/control-gate
uv sync --locked
uv run ruff check src tests
uv run pytest -q
```

최신 T5 계약 사례(서버 정본 조회 실패·위조 포함), 누락/중복/불변조건·예외 위조/만료·계획 변경·키 폐기·부정 인가·시각 경과·구조화된 네트워크 대조군을 시험한다. 고정 입력·시계에서 판정은 결정적이다. 실환경 AWS/RKE2/CNPG/T14 시험 실행을 대신하지 않는다. 이 변경은 자원을 생성·삭제하지 않는다.


## 보안·데이터 준비 결합

`CombinedGateService`는 별도 `combined-gate.v1` 외피에 기존 T5 `gate_comparison`과
`data-readiness-result.v1`을 넣는다. 보안 스키마의 시험 ID 범위를 몰래 확장하지 않는다.
둘 다 PASS여야 전체 PASS이며, 보안 예외가 데이터 BLOCK/UNKNOWN을 덮지 못한다.

호스트는 인증된 현재 context로 `ReadinessBinding`과 `RunSnapshot`을 조회한다.
`binding_provider(actor, ctx)`, `snapshot_provider(actor, ctx, binding)`,
`authenticate_binding`, `authenticate_snapshot`은 필수이며 기본 허용 구현은 없다.
권한·출처 검사에 기한이 있으면 `VerifiedAuthority`로 반환한다. 서명 검증기를 사용할 때
목적은 `readiness-binding`, `readiness-snapshot`이고 각 subject는 해당 객체 지문이다.

- 서버 정책: 버전, 정확한 스키마 해시 규칙, 스키마명 포함 필수 테이블, 시퀀스 컬럼,
  허용 복제 지연(bytes), 관측 최대 나이. 사용자 입력으로 범위를 축소하지 않는다.
- 원본·대상: 등록 환경 ID/ref·DB ref, 관측/만료 시각, 스키마 규칙, 테이블별 스키마 해시와 PK 목록.
- 복제: 같은 DB 쌍의 방향·관측 시각·lag bytes. None/음수/boolean은 0으로 보지 않는다.
- 시퀀스 리허설: source/target next, target max ID, proposed next, 실제 리허설 readback next.
  증가폭 1·비순환의 양수 시퀀스만 지원한다. 제안값은 두 next와 max ID+1보다 작을 수 없고,
  readback은 제안값과 같아야 한다. max ID의 null은 빈 표에만 쓰고 조회 실패는 next/readback을
  미확인으로 기록한다. 누락·중복·다른 테이블·미지원 시퀀스는 통과하지 않는다.

`assess_readiness()`는 DB에 접속하지 않는다. T13 생산자가 실제 관측과 리허설 증거를 보존해야 한다.
스키마 해시 계산은 T13 소유이며 SQL/Python 방식이나 numeric 동등성을 이 패키지가 재정의하지 않는다.
현재 예제의 `registered-schema-rule.v1`은 시험용으로만 쓰며 실제 규칙을 확정했다는 뜻이 아니다.
실제 정책의 tables/sequence_columns가 앱의 전체 이전 범위를 덮는지는 등록 단계에서 확인해야 한다.

`require_prerequisites()`는 현재 원본·정책·권한·서버 보안 정본을 다시 읽고 5초 이하의
`GatePrerequisites`를 반환한다. 보안·데이터·서명 중 가장 빠른 만료를 넘지 않는다.
`authorizes_cutover`는 False다. T10의 계획 잠금 안에서 실행 승인·SHIFT·보류·실행권을
결합해야 하며 이 패키지만으로 원자적 실행을 주장하지 않는다. 데이터 준비 PASS도 T12/T13의
쓰기 정지 이후 최종 동등성 비교·barrier·시퀀스 재확인을 대신하지 않는다.

시험은 인증 공급자 대역과 합성 데이터, 실제 T14 Runner 코드의 상호운용을 사용한다.
실제 EKS/RKE2 데이터 수집, T10 운영 정본 저장소·인증/현재 정책, 실제 컷오버는 아직
연결하지 않았다. 아래 선택 어댑터는 T13 SQL 규칙과 로컬 PostgreSQL 관측을 연결하지만
양쪽 팀 환경의 전체 T19 완료 조건은 열린 상태로 유지한다.


## T10 승인·실행 접수 어댑터

`bravo_gate.t10.T10GateAuthority`는 T10 `CutoverService.authority`에 연결한다.
기존 운영 권한·SHIFT·보안 보류·실행 epoch 검사 공급자 `upstream`을 필수로 받아
T19를 추가한다. T19가 운영 승인이나 실행권을 대신 발급하지 않는다.
선택 의존성 `bravo-control-gate[t10]`을 설치하고 같은 버전의 `bravo-platform-events`를 제공한다.
저장소 개발 환경에서는 uv가 인접한 platform-events 패키지를 사용한다.

```python
from bravo_gate.t10 import T10GateAuthority

# 두 공급자는 호스트의 인증된 서버 구현이다. 기본 허용 공급자는 없다.
authority = T10GateAuthority(
    upstream=current_operational_authority,
    load_session=load_registered_plan_gate_session,
)
# CutoverService(..., authority=authority, check_access=...)에 전달한다.
```

`load_session(connection, principal, plan)`은 T10이 전달한 **동일 트랜잭션**에서
인증된 tenant·plan ID로 등록을 읽고 `PlanGateSession`을 반환한다.
세션에는 `service`, `plan_id`, `context_id`, `context`, 불변 `report`, `readiness`,
서버 예외 `approval_ids`가 들어간다. service의 모든 조회·권한·폐기 콜백도 같은
계획 잠금 경계에 연결한다. 요청 본문을 결과 등록으로 쓰지 않는다.

| 접수 조건 | 검사 및 실패 처리 |
| --- | --- |
| 계획·테넌트·앱 | 서버 계획과 세션·context·T10 binding/command를 대조. 다른 계획에 재사용 거부 |
| 대상 자원 | 양쪽 환경 ID와 등록 DB 참조, gate/context/plan hash 대조 |
| 현재 결과 | 보안 정본·데이터 준비·권한·예외·기한 재검증. BLOCK/미확인/만료는 승인·실행 거부 |
| 검사 중 변경 | 운영 판단과 등록을 다시 읽어 변경 또는 5초 이하 전제조건 만료 시 거부 |
| 승인 후 변경 | 정확한 결과·정책·등록의 지문을 T10 승인 context에 포함. 새 PASS라도 재승인 필요 |
| 응답 유실 | 기존 T10의 durable claim·SAFE_HOLD·조회 전용 복구 유지. 자동 재실행 금지 |

5초짜리 전제조건은 승인 이후 저장해 재사용하지 않고 매 접수 때 다시 발급한다.
검사 시각만 바뀌어도 사람의 승인이 매번 무효화되는 문제를 피하기 위해, 승인 지문은
정확한 등록 결과와 운영 revision에 결속한다. 실제 원본·정책·권한 변경은 계속 차단한다.
검사 후 실행 claim까지의 트랜잭션 시간 제한과 실행기의 기한·epoch 확인은 T10/T12 책임이다.

### 운영 인계에서 반드시 검증할 사항

1. 등록·권한 회수 writer와 소비자가 같은 plan lock을 사용하고 tenant를 먼저 제한한다.
   Python 콜백이 실제로 올바른 DB 격리를 구현했는지는 이 어댑터가 자동 증명하지 않는다.
2. `load_session`과 모든 하위 공급자의 timeout·재시도·정본 인증 및 운영 로그인 연결을 시험한다.
3. 실제 T13/T14 입력을 공급하고 SHIFT 관측·보류·승인 폐기를 같은 접수 경계에서 확인한다.
4. T12 Executor의 최종 쓰기 정지 비교와 fencing, 응답 유실·프로세스 재시작 복구를 실환경에서 검증한다.

`tests/test_t10_interop.py`는 **실제 T10 SQLite/PostgreSQL 저장소와 승인·접수 코드**를 사용한다.
PostgreSQL은 임시 Unix socket 서버이며 비관리자 runtime 역할을 사용한다. T10 소유 시험
bootstrap을 재사용하며 기존 서버에 접속하지 않는다. 결과 저장은 테스트용 append-only
기록으로만 연결한다. 운영 기록 스키마·API를 새로 정의한 것이 아니다.
통제/데이터 관측과 운영 정책은 합성 fixture, Executor는 기록용 대역이다.
따라서 이 시험은 실제 클라우드 컷오버·운영 인증·외부 서비스 사이의 원자성을 입증하지 않는다.

전체 시험에는 PostgreSQL 서버 도구(`pg_config`, `initdb`, `pg_ctl`, `postgres`)가 필요하다.
도구가 없으면 PostgreSQL 시험을 생략하지 않고 실패한다.

## T13 실제 스키마 관측 어댑터

`bravo_gate.t13.collect_schema`는 등록된 DB의 전용 연결로 T13
`bravo_cutover.db_fingerprint.schema_fingerprint`를 호출한다. 해시 규칙을 별도로 구현하지 않는다.
선택 의존성 `bravo-control-gate[t13]`을 사용한다. 해당 T13 구현이 포함된 패키지 버전과
실제 설치 이미지 digest를 배포 시 함께 고정해야 한다.

- 서버가 선택한 context·정책의 환경/DB 등록과 정확히 일치해야 연결한다.
- 실제 psycopg 연결의 `verify-full`·TLS 사용·idle/autocommit을 검사한다.
- DB 신원을 읽기 전에 해당 트랜잭션의 `search_path`를 `pg_catalog`로 고정한다.
  `pg_catalog.current_database`, session/current role과 등록을 대조하고 superuser/BYPASSRLS를 거부한다.
  앱 스키마의 동명 함수로 다른 DB를 등록된 DB로 위장할 수 없어야 한다.
- T13의 읽기 전용 반복 가능 읽기·고정 세션 설정·시간 제한을 그대로 사용한다.
- 스키마/PK를 다시 읽어 수집 중 DDL 변화는 UNKNOWN으로 남긴다. 연결·신원·전체 기한 실패는
  앞서 읽은 부분 성공도 버린다. 반환되는 시각은 수집 시작 기준이며 최대 30초만 유효하다.
- DB 행, DSN, SQL 오류 원문을 결과에 넣지 않는다. 확인하지 못한 테이블은 누락시키지 않고
  hash/PK를 null로 남겨 기존 T19가 BLOCK하게 한다.
- `SchemaCapture.errors`는 T13의 허용된 고정 코드만 보존한다:
  `table_not_qualified`, `table_missing`, `unsupported_major`, `not_plain_table`,
  `primary_key_missing`, `unsupported_type`, `collation_unverified`,
  `statement_timeout`, `snapshot_unreadable`. 그 외 예외/미등록 코드는 기존
  `SCHEMA_UNCONFIRMED` 또는 재조회 시 `SCHEMA_CHANGED_OR_UNCONFIRMED`로 축약한다.
  원인 구분은 진단용이며 UNKNOWN/BLOCK을 완화하거나 승인 근거로 사용하지 않는다.

`DatabaseRegistration`은 인증된 호스트의 등록이다. 브라우저 JSON을 그대로 변환하지 않는다.
연결 생성자는 실제 클러스터 UID·호스트·CA·Secret 소유권을 해당 등록에 결속해야 한다.
같은 DB 이름과 인증서만으로 클러스터 신원을 증명하지 않는다. 연결 시간 제한과 외부 워커의
전체 종료 한도도 호스트가 강제한다. 이 함수의 시계 재검사로 막힌 소켓을 강제 종료하지는 못한다.

`assemble_snapshot`은 두 스키마 관측과 **호스트가 인증한 별도 복제·시퀀스 리허설 기록**을
기존 `data-readiness.v1`로 묶는다. 실제 수신한 복제 위치나 시퀀스 readback이 없으면
미확인으로 남겨야 한다. 제안한 setval 값을 관측값으로 대신 넣으면 안 된다.
이 모듈은 시퀀스 보정·DB 변경·복제 시작을 하지 않는다. 생성 결과도 기존 snapshot
인증/불변 저장·T10 접수 경계를 거쳐야 하며 객체 생성만으로 실행 권한을 얻지 않는다.

### 어댑터 검증과 미검증 범위

고정 digest PostgreSQL 18.4에서 실제 TLS와 비관리자 SELECT 역할을 사용했다.
스키마 일치 → 대상 DDL 불일치 BLOCK, 수집 중 DDL UNKNOWN, TLS 미사용/검증 생략/
잘못된 호스트/다른 DB/관리자 역할/진행 중 트랜잭션 거부, 만료 관측 BLOCK을 시험했다.
이 시험은 실제 스키마 수집을 검증하며, 복제 기록과 권한 공급자는 합성 대역이다.
팀 EKS/RKE2·실제 복제 지연·시퀀스 리허설·운영 승인·컷오버를 검증한 결과가 아니다.

추가 통합 시험은 한 임시 PostgreSQL 프로세스의 서로 다른 DB 사이에서 실제 TLS 논리 복제를
실행한다. 원본 행이 복제된 뒤에도 목적지 시퀀스가 뒤처지는 것을 확인하고, 제안값만 있는
경우 BLOCK, T13 보정 후 실제 readback이 있는 경우 준비 PASS, 복제 관측이 미확인인 경우
BLOCK을 확인한다. 이 시험의 허용 WAL 지연은 1 MiB이며 RPO 0나 무중단 자격이 아니다.
운영 앱은 없고 신원·정책 정본은 시험 fixture다. 실제 두 환경의 네트워크·writer fencing·
트랜잭션 drain·최종 데이터 비교와 전체 T19/T10 승인을 대체하지 않는다.

`tests/test_t13_two_servers.py`는 독립된 PostgreSQL 서버 두 대와 서로 다른 CA·접속 비밀을
사용한다. 두 system_identifier가 다르고 복제 연결이 TLS임을 실제 SQL로 확인한다.
50행 복제·T13 지문 일치, 시퀀스 제안만 있을 때 BLOCK, 보정 readback 후 준비 PASS,
복제 중단 뒤 원본 51행/대상 50행에서 BLOCK을 확인한다. DB명과 역할명이 같아도 목적지
CA로 다른 서버에 연결하면 스키마를 확인할 수 없는 상태로 남는다.
이것은 로컬 Docker 브리지·루프백 포트를 사용한 시험이며 팀 EKS/RKE2나 운영 신원 발급을
검증하지 않는다. 해당 시험 전용 컨테이너·네트워크만 생성하고 종료 시 ID로 정리한다.

2026-11-04에는 [실제 출발지 앱 DB의 스키마 수집](../../docs/evidence/t19-source-readiness-2026-11-04/README.md)을
추가 확인했다. 두 테이블의 PK·스키마 지문 수집, 기대 DB 이름 불일치 시 UNKNOWN,
목적지·복제·시퀀스 증거 누락 시 BLOCK이다. 출발지 읽기 자격 시험이며 양쪽 환경 게이트 인수는 아니다.
