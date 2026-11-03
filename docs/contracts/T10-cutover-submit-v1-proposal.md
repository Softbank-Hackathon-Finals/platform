# T10 CUTOVER 접수와 실행 관측

현재 구현은 **신뢰된 호스트에 붙이는 승인·접수 어댑터**다. T19 정본 조회, T17 SHIFT, 사용자 로그인, 전체 계획 상태 머신의 구현이나 실제 컷오버 완료를 뜻하지 않는다. 설계 FR-7, 8.2, 8.6, 8.9와 [T5 소비 계약](../controls/README.md)을 따른다.

## 승인과 실행 순서

1. 호스트가 검증한 `Principal`로 현재 tenant의 계획을 찾는다. 클라이언트 tenant 헤더는 인증으로 사용하지 않는다.
2. 계획이 `SHIFT`이고, `CutoverAuthority`가 동일한 계획 쓰기 잠금 안에서 현재 실행 조건을 검증해야 승인할 수 있다. 이 어댑터에는 계획을 SHIFT로 건너뛰는 API가 없다.
3. `POST /v1/plans/{id}/approvals`는 `approval_kind=cutover`, 현재 `binding`, `expected_version`, `Idempotency-Key`를 받는다. 승인 ID, 승인자, 시각, 계획 버전, 서버의 불변 명령과 authority revision을 이벤트와 함께 저장한다. S2 `change` 승인은 거부하며 T15/T31에 대한 승인을 발급하지 않는다.
4. `POST /v1/plans/{id}/cutover`는 승인 ID와 새 `Idempotency-Key`만 받는다. 클라이언트는 epoch, 실제 DB 접속 참조, deadline이나 실행 예산을 정하지 않는다. 현재 승인자의 권한·폐기 여부와 실행 요청자의 권한을 다시 검사하고 현재 계획·diff·이미지·게이트·만료·설정 revision을 저장된 승인과 대조한다.
5. 명령 ID, 승인 소비, 계획 내 epoch 사용, `SHIFT → CUTOVER` 및 이벤트를 기존 DB 트랜잭션에 저장한다. 커밋에 성공한 최초 호출자만 이 명령 ID를 Executor 멱등 키로 사용해 POST 한 번을 보낸다.
6. 같은 요청 재시도나 프로세스 재시작은 저장된 명령을 찾아 GET으로만 관측한다. POST 응답을 잃거나, 커밋 뒤 전송 전에 프로세스가 죽으면 자동 재전송하지 않는다. 빈 조회 결과도 UNKNOWN이다.
7. 관측된 실행 ID·계획·epoch·이력의 일관성을 확인하고 단계별 `execution.updated`를 같은 DB 트랜잭션에 저장한다. 키는 실행 ID와 단계 ordinal에 결속되며, 시각은 최초 관측 시각이다. 반복 조회는 이벤트를 다시 만들지 않는다.

`POST /v1/plans/{id}/cutover/{command_id}/reconcile`는 같은 조회·저장 경로를 명시적으로 실행한다. 새 원격 명령을 보내지 않는다. 기존 계획 GET과 SSE에 같은 principal 정책을 사용한다. 반환 관측값은 allowlist를 따르며 원격 detail/result, 접속 참조와 자격은 반환하지 않는다.

## 호스트가 제공해야 하는 검증

`CutoverAuthority(connection, principal, plan, now, approved_by, approval_id)`는 검증 후 `CurrentCutover(binding, command, revision)`을 반환한다. **이 객체 생성 자체는 검증이 아니다.** 기본 구현은 항상 거부하고 HTTP 503을 반환한다. 지금 저장소에는 이 포트에 연결할 운영 T19/SHIFT 정본 저장소가 없다.

호스트는 다음을 읽고 검증해야 한다.

- 인증된 T19 서비스의 불변 정본·현재 평가 context·전체 판정·필수 행·현재 예외의 유효성. 입력 hash나 이벤트의 PASS만으로 통과시키지 않는다.
- 현재 계획, diff, 배포 이미지 digest, 목적지, 게이트 ID와 context digest, 승인 만료와 폐기, 원래 승인자 및 현재 요청자의 권한.
- 실제 SHIFT 100% 및 지연 기준 충족, 데이터 준비, 보안 보류와 실행권, 등록 DB 자원·접속 참조·설정 revision.

이 조건을 바꾸는 모든 writer는 같은 계획 잠금을 사용해야 한다. 원격 조회를 잠금 전에 캐시한 결과나 boolean만 돌려주는 대체 구현은 통합 검증을 충족하지 않는다. 승인 소비와 원격 실행 사이의 결정 시점은 **명령 접수 커밋**이다. 이미 접수한 원격 명령은 승인 폐기로 취소됐다고 표시할 수 없다.

## 저장·복구와 전송

- 기존 `plans`, `plan_commands`, `events`와 SQLite 쓰기 트랜잭션 / PostgreSQL 계획 advisory lock을 재사용한다. 스키마 변경이나 runtime 권한 확대는 없다. journal key는 전용 `cutover-` 접두사와 해시를 쓴다. 실행 관측은 불변 행으로 추가하며 기존 명령 영수증을 덮어쓰지 않는다.
- 조회 불명 또는 원격 SAFE_HOLD는 계획을 SAFE_HOLD로 저장한다. 이후 원격 DONE을 읽어도 보류를 자동 해제하거나 계획을 WATCH/DONE으로 올리지 않는다. 무결성 영수증·WATCH·완료 판정은 후속이다.
- 실행별 단계 이력의 변경·퇴행은 UNKNOWN이다. 원격 생성/갱신 시각을 개별 단계 발생 시각으로 바꾸지 않는다.
- Executor는 서버 설정의 한 DB 쌍과 connection_ref만 받는다. 이 어댑터의 계획별 epoch 기록은 T12의 실제 자원별 lease·단조 epoch 검사를 대체하지 않는다.
- 전송은 검증된 HTTPS와 메모리의 Bearer를 사용한다. 전용 CA는 `ssl.create_default_context(cafile=...)`로 만든 `SSLContext`로 주입한다. 인증서 검사와 hostname 검사를 모두 요구한다. 클러스터의 평문 HTTP는 허용하지 않는다. literal loopback HTTP만 시험용으로 유지한다.
- timeout, 오류 상태, 잘못된 응답, 초과 크기, 중복 JSON key는 UNKNOWN이다. POST 재시도·redirect·환경 프록시는 없다. 승인/명령 저장 실패 시 POST를 보내지 않는다.

## 검증과 남은 연결

합성 authority와 시험 전용 SHIFT seed는 tests에만 있다. SQLite와 실제 임시 PostgreSQL에서 승인 만료·폐기·현재 결속 변경, 타 tenant, 중복/동시 요청, 저장 실패, 커밋 후/응답 후 프로세스 유실, 재개 시 GET, 이벤트 중복 방지와 SAFE_HOLD 보존을 확인한다. 실제 loopback TCP 응답 유실과 전용 CA의 정상/미신뢰/잘못된 hostname도 시험한다.

운영 인증, T19/SHIFT authority, 실제 TLS Executor, 원격 DB·앱·T13 영수증을 연결한 통합 시험은 남아 있다. 현재 계획 모델을 SQL로 SHIFT에 바꾸거나 합성 gate/승인을 운영에 넣어 그 조건을 통과시키지 않는다. T10 전체 이슈는 열어 둔다.
