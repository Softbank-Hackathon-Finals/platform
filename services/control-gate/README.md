# T19 통제 게이트

병합된 T5의 프로필·시험 불변조건·서버 예외 승인 계약을 실행하는 결정적 Python 모듈이다. 보안 게이트 계산과 T10/T21의 소비 검증을 제공한다. LLM·클라우드 변경·시험 실행·예외 승인 발급은 하지 않는다.

**범위:** T19의 보안 전제조건. `CombinedGateService`는 T13 데이터 준비 관측과 보안 결과를 함께 검사한다. 실제 T13 수집기·T14 환경 연결·T10 운영 승인 및 실행권 통합은 별도다. `SecurityPrerequisite.authorizes_cutover`는 항상 `False`다. T19 이슈 전체의 완료나 실환경 동등성 검증 완료로 간주하지 않는다.

## 계약 출처

`src/bravo_gate/contracts/`는 develop `626bca0`의 `docs/controls/` 계약을 고정한다. 스키마와 프로필은 기존 T5 고정본과 바이트 단위로 같고, 최신 서버 정본 소비 사례도 함께 가져왔다. 경로·커밋·해시는 `provenance.json`에 있다. 원본과 패키지 사본의 일치 여부를 시험한다. 계약 변경은 상호운용 시험과 해시 갱신을 함께 수행한다.

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

`demo-gate`는 3개, `full`은 20개 시험을 요구한다. `full`에서 DR-01.1이 없으면 항상 BLOCK이며 예외로 무시할 수 없다. 운영 전환에서 어느 프로필을 요구하는지는 T10의 서버 정책이 선택한다. UI가 작은 프로필로 바꿔 통과시키는 API는 제공하지 않는다.

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

NET-01.2/01.3의 PASS에는 `NetworkProofAuthority`가 필요하다. 증거 `output_sha256`에 연결한 구조화된 원본을 등록된 저장소에서 읽으며, URI를 임의로 가져오거나 `excerpt`를 정규식으로 해석하지 않는다.

- 서버의 `NetworkBinding`이 목적지·시험 주체·허용 주체·허용 서비스·DNS·정책 설정 hash·접속 제한시간을 고정한다.
- 연결 거부 또는 설정한 제한시간까지의 timeout을 확인한다.
- 같은 DB/포트에 대한 허용 주체의 성공, 같은 시험 주체의 허용 경로 성공, NET-01.3의 DNS 성공을 같은 60초 창에서 확인한다.
- 대조군 실패·다른 대상·다른 주체·오래된 증거·설정 hash 불일치·원본 변조는 통과시키지 않는다.
- 확인한 근거와 등록의 기한을 보안 전제조건 기한에도 반영한다.

이것은 선정한 도달성 시험을 해석하는 계약이다. 타임아웃의 모든 원인을 완벽하게 규명하거나 네트워크 전체의 안전을 보장하지 않는다. T14가 구조화된 근거와 인증된 등록을 제공하지 않으면 이 시험은 미검증으로 BLOCK된다.

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
실제 EKS/RKE2 데이터 수집, T10 정본 저장소·인증/현재 정책, T13 규칙 확정과 SQL 관측,
실제 컷오버는 아직 연결하지 않았으며 T19 완료 조건은 열린 상태로 유지한다.
