# T19 원문을 T10 계획에 저장하기

`bravo_platform_events.gate_records.GateRecords`는 기존 계획 잠금과
`plan_commands`에 통제 시험 원문, 현재 게이트 컨텍스트, 판정 원문을 저장한다.
T19의 판정이나 인증 규칙을 복제하지 않는다. 새 DB·테이블·관리 API도 만들지 않는다.

운영 호스트는 기존 `bravo-control-gate[t10]`와 같은 버전의 T10을 설치한다.
T10 기본 HTTP 서비스는 이 선택 모듈을 자동으로 설치하거나 노출하지 않는다.
호스트가 생산자 인증을 마친 뒤 `record_run()`을 호출한다. `authorize`는
같은 연결에서 현재 주체·테넌트·계획과 작업 권한을 확인하고 정확히 `True`를
반환해야 한다. `gate:record`, `gate:bind`, `gate:evaluate`, `gate:read`를 구별한다.
운영자의 `read` 권한만으로 등록 권한을 주면 안 된다.

1. `record_run(principal, plan_id, side, RunSnapshot)`으로 원문을 저장한다.
   한쪽만 먼저 도착해도 저장할 수 있다. 같은 참조·같은 내용은 멱등이고
   같은 참조의 다른 내용은 거부한다. 원문 정규화·해시는 T19 함수를 사용한다.
2. 서버 정책이 선택한 `GateContext`를 `bind()`에 전달한다. 앱·계획 digest·
   양쪽 환경과 원문 참조·해시를 대조한다. 목적지가 없으면 연결을 거부한다.
   `expected_revision`으로 이전 상태에 덮어쓰는 것을 막는다. 오래된 요청의
   재시도는 이전 영수증만 반환하고 이전 컨텍스트를 다시 활성화하지 않는다.
3. `evaluate()`의 `service_factory(view)`는 기존 `CombinedGateService`를
   구성한다. `view.context_provider`, `run_provider`, `report_provider`는
   같은 연결을 사용한다. T19의 인증·현재 권한·예외·T13 준비 판정 공급자는
   호스트가 별도로 제공해야 한다. 저장했다는 이유로 인증을 `True`로 바꾸지 않는다.
4. 평가가 끝나면 보안 원문과 합산 판정이 함께 저장된다. 새 측정이나 판정은
   새 컨텍스트와 gate ID를 사용한다. 저장 실패 시 두 원문 모두 롤백된다.
5. `T10GateAuthority.load_session` 안에서 `records.view(connection, principal, plan)`을
   만들고 `view.session(combined_service, readiness_binding)`을 반환한다.
   `read_run()`은 목적지 도착 전에도 인증된 주체가 출발지 원문을 읽는 접점이다.

모든 공급자는 **호출자의 계획 트랜잭션 안에서만** 사용한다. HTTP 입력의
계획·프로필·권한 문자열을 운영 정본으로 등록하지 않는다. 운영 호스트는
실제 환경 등록, 생산자 원문 인증, T13 정책·관측, 보류·승인·SHIFT·Executor를
여전히 연결해야 한다. 저장된 PASS는 현재 실행 허가가 아니다.

## 검증과 운영 경계

`tests/test_gate_records.py`는 SQLite와 실제 로컬 PostgreSQL에서 저장·재접속,
목적지 누락, 참조 교체, 테넌트·앱·환경 불일치, 권한 회수, 컨텍스트 CAS,
T19 판정·생산자 신뢰 거부를 검사한다. 통제 시험 입력과 인증 주체는 합성이다.

`gate_records_probe.py`는 기존 T7 DB runtime 자격·CA를 읽기 전용으로 마운트한
한 번의 클러스터 Job에서 실행한다. `BRAVO_PROBE_ID=t10-gate-probe-<고유값>`을
사용하고, DB TLS·제한된 runtime 역할·영속 저장·재접속·권한 회수·목적지 누락을
검사한다. 기존 업무 DB나 계획을 변경하지 않으며 Executor를 호출하지 않는다.

이 probe는 `verification-only` 테넌트에 **UNKNOWN/NOT_RUN인 합성 출발지 입력만**
저장한다. 목적지 실측을 만들어 채우지 않는다. 원문과 권한 회수 이력은 기존
append-only 정책대로 남긴다. Job·ConfigMap·전용 NetworkPolicy는 실행자가
생성 응답 UID로 정리하고 부재를 확인한다. 공유 Secret·권한·Deployment는 바꾸지 않는다.
클러스터의 저장 경로 통과를 실제 양쪽 T14 결과, T19 전체 PASS, 사람 로그인이나
컷오버 E2E의 완료로 보고하지 않는다.

정본: [T19 서비스](../../control-gate/README.md),
[T10 컷오버 접수](../../../docs/contracts/T10-cutover-submit-v1-proposal.md),
[T19 운영 인수 #29](https://github.com/Softbank-Hackathon-Finals/platform/issues/29).
