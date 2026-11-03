# T19 통제 게이트

T5 PR #51의 프로필·시험 불변조건·서버 예외 승인 계약을 실행하는 결정적 Python 모듈이다. 보안 게이트 계산과 T10/T21의 소비 검증을 제공한다. LLM·클라우드 변경·시험 실행·예외 승인 발급은 하지 않는다.

**범위:** T19의 보안 전제조건. T13 데이터 경로 준비, T14 실제 시험기, T10 운영 승인·실행권과의 결합은 별도다. `SecurityPrerequisite.authorizes_cutover`는 항상 `False`다. T19 이슈 전체의 완료나 실환경 동등성 검증 완료로 간주하지 않는다.

## 계약 출처

`src/bravo_gate/contracts/`는 platform PR #51의 `54c2ce893b6475ca0b59abe09cd8fe369e0e4b9b`에서 가져온 JSON Schema와 프로필이다. 원본은 `docs/controls/`이며, PR이 아직 main에 들어오지 않아 패키지를 독립적으로 검증할 수 있도록 고정 사본을 둔다. 경로·커밋·해시는 `provenance.json`에 있다. 원본 계약 변경은 자동 수용하지 않는다. 계약 변경·프로필 버전 갱신은 상호운용 시험과 해시 갱신을 함께 한다.

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
| 예외 결속 | tenant·계획·gate·profile hash·양쪽 원본 결과 hash까지 서버 context digest에 결속 |
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
| `authenticate_context/run/approval` | 각 발급 역할·원본·범위·현재 신뢰를 검증. 명시적 False나 truthy 객체는 거부 |
| `authorize` | 현재 사용자/서비스의 평가·예외 사용 권한 검사. `approved_by` 표시값과 구분 |
| `clock` | 서버 UTC 시계. 요청 JSON에서 평가 시간을 고르지 않음 |

```python
report = service.evaluate(actor, context_id, approval_ids=("server-approval-id",))
# T10은 저장한 report와 context를 다시 읽고 실제 실행 직전에 호출한다.
security = service.require_security(actor, context_id, serialized_report,
                                    approval_ids=("server-approval-id",))
# security는 최대 5초·원래 근거 기한 안의 보안 전제조건이다.
# 데이터 준비·운영 승인·현재 상태·실행권이 별도로 통과해야 컷오버 가능하다.
```

`Ed25519Authority`는 계획 context·문서 hash·발급 목적을 기존 서버 공개키 원장과 대조하는 구체 서명 검증기다. 키 폐기·교체·역할 불일치를 재검증한다. 평가기에 서명 개인 키를 제공하지 않는다. 입력 JSON의 `evidence.signature` 필드만으로 인증하지 않는다. 서명은 발급 출처를 확인하며, 실제 시험이 제대로 실행됐다는 사실은 T14 수집기 자격으로 검증해야 한다.

예외 승인 생성은 T10에서 승인자 권한·분리 승인·감사 기록을 확인한 후 수행한다. T19는 그 기록을 읽기만 한다. 임의 JSON 승인 배열이나 사용자 문자열을 DB 승인으로 전환하지 않는다. 소비 문서의 예외는 서버 기록과 완전히 일치해야 한다. 승인 폐기 또는 계획/원본 변경 뒤 예전 PASS 문서를 재사용할 수 없다.

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

## 검증

```sh
cd services/control-gate
uv sync --locked
uv run ruff check src tests
uv run pytest -q
```

T5 계약 사례 28건, 누락/중복/불변조건·예외 위조/만료·계획 변경·키 폐기·부정 인가·시각 경과·구조화된 네트워크 대조군을 시험한다. 고정 입력·시계에서 판정은 결정적이다. 실환경 AWS/RKE2/CNPG/T14 시험 실행을 대신하지 않는다. 이 변경은 자원을 생성·삭제하지 않는다.
