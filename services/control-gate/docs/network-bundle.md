# T14 네트워크 원문 → T19 인수

## 변경 이유

T14의 NET-01.2/01.3은 DB와 풀러를 모두 시험한다. 기존 T19 네트워크 검증기는 등록된 한 경로의 대조군을 확인했지만, 이 결과가 두 경로 전체를 확인했다는 표시는 없었다. 단일 경로 증명을 전체 시험에 사용하지 않도록 하고 실제 T14 집계 형식의 인수 어댑터를 추가한다. 운영 우회를 실증했다는 의미는 아니다.

## 호스트 연결

1. 현재 계획의 `GateContext`와 인증된 양쪽 `RunSnapshot`을 기존 경로로 읽는다.
2. 각 NET 시험에 `NetworkRunRegistration`을 서버에서 등록한다. context digest, side, app, environment ref, run ID, test ID, method version, 기한과 DB·풀러의 `NetworkPathRegistration` 두 개를 포함한다.
3. 경로 등록 hash는 호스트가 확인한 리스너·시험 주체·정상 대조 경로·DNS와 결속해야 한다. 실제 Service/EndpointSlice/Ready Pod 및 정책 출처를 확인하지 않은 클라이언트 입력은 등록하지 않는다.
4. `binding_provider`는 위 불변 등록을 반환한다. `artifact_provider`는 요청된 digest의 **원본 bytes**만 반환한다. 저장소 URI나 임의 파일 경로를 사용자 입력으로 받지 않는다.
5. `authenticate_binding`과 `authenticate_artifact`는 tenant·계획·현재 권한 및 출처를 확인한다. 만료가 있으면 기존 `VerifiedAuthority`를 반환한다. 집계 및 개별 원문은 각각 16 KiB 이하이며 정확히 3개다.
6. `CollectedNetworkProofAuthority`를 `GateService(network_verifier=...)`에 주입한다. 방법 2에서는 원격 실행 시간이 코디네이터 요청 구간을 넘거나, 정상 대조군이 시험 요청의 앞뒤에 위치하지 않으면 거부한다(정수 밀리초 반올림 1 ms 허용). 형식 누락·권한 회수·원문 변경·만료는 예외/미확인으로 처리한다. 과거 PASS로 대체하지 않는다.

`verify_collected_bundle`은 구조·시간·digest만 확인하는 순수 함수다. 이 함수의 성공을 출처 인증이나 실행 권한으로 사용하면 안 된다. `VerifiedNetworkProof`도 JSON 요청에서 역직렬화해 믿는 형식이 아니라 서버 검증기의 내부 반환값이다.

## 호환성

| 경로 | 결과 |
| --- | --- |
| 기존 단일 경로 원문 | 진단 검증 가능, 전체 NET PASS 불가 |
| T14 형식 1의 DB 5432·풀러 6432 원문 | 양쪽 등록·원문·대조군 검증 후 가능 |
| 역할별 리스너 형식 2 | 파서 준비. 별도 방법/프로필 승인 및 계획 등록 전 운영 판정 불가 |
| 집계 PASS만 있고 자식 원문 없음 | BLOCK / UNKNOWN |
| 실제 한 경로라도 allow | 전체 PASS 근거로 거부 |
| 미확인 목적지 또는 source 결과 재사용 | BLOCK |

방법 2는 T14 [PR #436](https://github.com/Softbank-Hackathon-Finals/platform/pull/436)의 후보 계약이다. 기본 T5 카탈로그는 방법 1을 유지하므로 방법 2를 전달하면 기존 방법 불일치 차단이 유지된다. 운영 승인을 대신해 자동으로 카탈로그를 확장하지 않는다.

### 소비자 시험 데이터의 이행

서버 검증기를 모의 구현한 소비자 시험에서도 `VerifiedNetworkProof`는
`required_roles_verified=("database", "pooler")`와 해당 side의 실제 `RunSnapshot.document["run_id"]`를
반환해야 한다. 기본 빈 값은 기존 단일 경로 진단을 의미하며, 전체 NET 통과로 승격하지 않는다.
운영 호스트가 검증 없이 이 두 필드만 채우는 것은 허용되지 않는다.

T10의 기존 `test_application_gate.py` 합성 authority는 이 두 필드가 없어 SQLite/PostgreSQL
두 시험에서 BLOCK되었다. T19 게이트 CI 통과와 별개로 이 소비자 CI는 실패 상태다.
별도 복사본에서 두 필드만 보완한 해당 파일 16개 시험은 로컬 실제 PostgreSQL과 SQLite에서
통과했다. T10 담당의 범위 합의 후 소비자 fixture를 연결해야 하며, 이 문서 변경은 T10 파일을
수정하거나 필수 CI를 우회하지 않는다.
[인계 기록](https://github.com/Softbank-Hackathon-Finals/platform/issues/29#issuecomment-6677547855).

## 검증

- 합성 원문으로 양쪽 역할·전후 대조군·DNS·최대 시간·digest·등록·권한 회수·run ID 불일치를 검사한다.
- 실제 T14 `NetworkAccessProbe`와 `Runner`가 생성한 형식 1 원문을 실제 T19 `GateService`로 전달한다. 연결 응답과 서버 출처 인증은 시험용 모의 구현이다. full 20개 합성 결과의 PASS 뒤 풀러 자식 원문을 제거하면 BLOCK하고 기존 판정을 소비할 수 없다.
- 방법 2 수집기 코드와 별도 로컬 연결 시험을 수행했다. 합성 정상 차단만 수용하고 DB 연결 허용·풀러 연결 허용·정상 대조군 실패는 모두 거부했다.
- 2026-11-06 로컬 검증: PostgreSQL 17.11/TLS PostgreSQL 18.4와 Docker를 포함한 게이트 시험 **404개 통과**, 네트워크·생산자 연결 관련 **110개 통과**, Ruff·공개 증빙·출처·비밀 검사 통과. 운영 계획 등록·목적지 수집·T10 영속 원문 수신 완료를 뜻하지 않는다.

## 운영 인수에 남은 조건

T10의 실제 계획별 원문 저장·조회와 현재 권한 검증에 연결하고, 승인된 양쪽 시험 환경에서 새 증거를 수집해야 한다. 11월 6일 출발지에서 관측한 NET-01.3의 실제 TCP 허용 결과는 FAIL로 유지한다. 저장된 증거의 만료 시간을 늘리거나 수집 시각을 현재로 바꿔 운영 PASS에 재사용하지 않는다.
