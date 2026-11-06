# SEC-01.4 비밀 회전 후 앱 회복 확인

[T5의 SEC-01.4](../../../docs/controls/README.md)를 구현하는 읽기 전용 검사기다.
T5 합격 기준이나 T19 판정 규칙은 바꾸지 않는다. `SecretRotationProbe`에 등록한
신뢰된 수집기가 완료된 회전 시험과 현재 상태를 공급한다. API 요청 JSON, 모델 출력,
콜백이 단순히 되돌려 준 이름이나 SHA-256만으로 실행·승인을 증명하지 않는다.

## 판정

1. 같은 실행·앱·환경·workload UID·DB UID·앱 역할·Secret 참조·revision에 결속한다.
2. 회전 전 직접 접속한 앱의 `/api/health/db` 200, 새 쓰기 201과 DB의 단일 행 저장을 확인한다.
3. 비밀 버전이 달라지고, 기존 자격의 새 인증은 거부되며 새 자격 인증은 성공해야 한다.
4. 그 앱의 기존 DB 세션을 종료한 근거가 있어야 한다. 다른 앱 연결을 종료하지 않는다.
5. 회전 적용 이후 시작한 **실제 쓰기 담당 DB 연결**로 새 표식이 정확히 한 번 저장돼야 한다.
   PgBouncer의 클라이언트 연결만 새로 생겼다는 정보, `/readyz`, 캐시 응답은 대체 근거가 아니다.
6. 단일 단조 시계로 회전 요청 직전부터 새 쓰기·DB 확인 완료까지 측정한다. 30초 이내,
   DB health 200, 쓰기 201, 앱 인스턴스·컨테이너·프로세스 시작 시각·재시작 횟수가 같아야 PASS다.
   이 측정은 제공자 회전 요청 지연도 포함한다. 실제 제공자의 장기 비동기 회전을 숨기지 않는다.
7. 검사 시점에 현재 등록·프로세스·활성 비밀 버전을 다시 읽는다. 시험 중 변경된 대상이나
   10분이 지난 시험, 단계 순서 오류, 벽시계와 단조 시계의 큰 차이는 수용하지 않는다.

실제로 재시작했거나 30초 회복 한도를 넘긴 완전한 관측은 FAIL이다. 아직 30초가 지나지 않은
503, 시험 도중 연결·권한·원문이 사라진 경우, 기존 세션 종료나 새 자격 적용이 불확실한 경우는
UNKNOWN이다. HTTP 200으로 재사용된 멱등 쓰기를 새 쓰기 201로 바꾸지 않는다.

`RotationTrial`과 `AppDatabaseCheck`는 신뢰된 호스트 내부의 자료형이다. `trial_evidence_sha256`은
호스트가 보존한 원문을 가리키며 서명이 아니다. 원문에는 정확한 실행·등록·긍정 대조군·버전 전환·
기존 세션 종료·HTTP 요청·DB 커밋의 상관관계가 있어야 한다. 비밀번호나 비밀번호 해시는 넣지 않는다.
T5 결과에는 식별자를 최소화한 요약과 원문 해시만 전달한다. 증거 저장이 실패하면 PASS를 반환하지 않는다.

## 실제 실행과 읽기 전용 검사의 경계

이 모듈은 회전 권한을 발급하지 않고 비밀번호·연결·Kubernetes 설정을 변경하지 않는다.
`Runner.registered_context`와 이미 저장된 회전 시험을 연결한다. 조회 재시도가 회전을 다시 실행하지 않는다.
운영 실행 호스트가 다음 조건을 충족한 뒤에만 완료 영수증을 만든다.

- 등록된 계획·앱·환경·자원과 합의된 시험 창에 대해 실행권을 한 번 확보한다.
- 변경 직전 등록과 권한을 재확인하고, 실행 전 의도를 내구 저장소에 기록한다.
- 응답 유실 시 원문·현재 상태를 읽어 조정한다. 무조건 다시 회전하거나 옛 비밀번호로 복구하지 않는다.
- 완료·불확실·복구 필요를 별도로 저장하고, 불확실한 상태에서는 후속 전환을 허용하지 않는다.
- 승인 범위 안의 앱 세션만 종료한다. 공유 DB 전체의 연결 종료·재시작은 시험 범위가 아니다.

현재 출발지의 `infra/envs/source-workloads/seed.py`는 초기 값 생성·일치 확인용이다.
CNPG 앱 역할 Secret, PgBouncer SCRAM 인증 파일, AWS Secrets Manager의 값이 서로 연결돼 있다.
이 중 하나만 변경하거나 기존 seed 명령을 회전 실행기로 재사용해서는 안 된다. 운영 회전은
T6와 각 저장소의 적용 순서·readback·실패 복구를 합의한 별도 실행 경로가 필요하다.
목적지는 T30의 OpenBao·ESO·DB·풀러 동기화 방식과 새 연결 반영을 별도로 확인해야 한다.

## 재현 가능한 로컬 자격 시험

`testing/secret-rotation.Dockerfile`과 `tests/integration/verify_secret_rotation.py`로 실행한다.
고정한 demo-chat Git 커밋을 `git archive`로 가져와 읽기 전용으로 마운트한다. 원본 checkout을 수정하지 않는다.
앱의 실제 HTTP·DB pool·인증 실패 후 갱신 코드는 그대로 실행한다. **Secrets Manager 읽기 함수만
로컬 파일 읽기로 대체**하며 S3 첨부 기능은 사용하지 않는다. 따라서 AWS/OpenBao 운영 회전 시험이 아니다.

```sh
docker build -f services/control-runner/testing/secret-rotation.Dockerfile \
  -t bravo-t14-rotation-qualification:20261106 services/control-runner/testing
PYTHONPATH=services/control-runner/src services/control-runner/.venv/bin/python \
  services/control-runner/tests/integration/verify_secret_rotation.py \
  --demo-repo /path/to/demo-chat --demo-revision FULL_40_CHARACTER_COMMIT
```

PostgreSQL 18.4의 서버 인증서·호스트명 검증, SCRAM 및 channel binding을 사용한다.
임시 DB trigger가 쓰기를 커밋한 backend 시작 시각을 독립적으로 기록한다. 이 trigger는 시험용이며
운영 DB에 추가하지 않는다. 정상 갱신, 실제 앱 재시작, 자격 공급원이 갱신되지 않는 30초 실패를 비교한다.
컨테이너는 비 root·capability 제거·no-new-privileges·외부 네트워크 없음·호스트 포트 미공개로 실행한다.
CA 개인 키는 호스트 밖으로 보내지 않고, 생성한 컨테이너와 임시 파일만 정리한다.

## 남은 운영 인수

실제 양쪽 Secret 제공자 회전·승인된 실행 호스트·PgBouncer 인증 갱신·기존 앱 연결 종료·
커밋 연결 상관관계·T10 운영 계획 저장을 연결해야 한다. 단위 시험이나 로컬 앱의 성공을
T14 양쪽 통과·T19 전체 PASS로 등록하지 않는다. 다른 통제 시험의 UNKNOWN도 유지한다.
