# T10 승인 호스트 실행

`approval_server`는 T10 승인·리허설 API와 기존 T15 구성 함수를 하나의 trusted
host에 연결한다. T15의 planner·제출기·원문 조회기를 같은 프로세스에 둔다.
T15 코드를 복제하지 않고 같은 저장소의 `bravo-rehearsal-runner` 패키지를 사용한다.

## 시작 입력

```sh
uv sync --locked --extra postgres --extra trusted-host
python -m bravo_platform_events.approval_server \
  --config /approval/host.json \
  --ports reviewed_deployment:create_ports \
  --tls-cert /tls/tls.crt --tls-key /tls/tls.key
```

`--ports`는 이미지에 포함하고 검토한 배포 모듈의 `create_ports(store)`다.
HTTP 입력이나 모델 출력으로 모듈을 선택하지 않는다. 이 함수는
`TrustedHostPorts`를 반환해야 한다. 운영 catalog·planner·게이트를 시험용
구현으로 대체하는 기본값은 없다.

| 입력 | 연결할 현재 구현·원본 |
|---|---|
| `catalog` | T15의 계획별 등록 작업 조회기. `for_plan(connection, plan, task_id)`와 `for_admission(admission)` |
| `planner` | T15 `BucketPlanner`: 목적지 profile·고정 시험 버킷·전용 자격 참조·인증된 T11 proposal source·현재 PlanBinding |
| `executor`·`bindings` | 기존 T12 `ExecutorClient`와 등록된 DB 환경 쌍. command/read 자격은 구분 |
| `cutover_authority` | 현재 T19 원문·SHIFT 관측을 같은 계획 트랜잭션에서 읽어 `CurrentCutover`를 반환하는 T10 접점 |

`load_host(store, config_file, create_ports)`는 아래 JSON의 정확한 필드를 받는다.
경로는 배포 소유자가 지정한 절대 경로다. 기록 디렉터리는 미리 마련된 영속
볼륨이어야 한다. 시작 함수는 DB migration·사용자 권한 등록을 하지 않는다.

```json
{
  "version": 1,
  "workload_binding": {
    "version": 1,
    "issuer": "https://confirmed-cluster-issuer.invalid",
    "audience": "bravo-t10-rehearsal-intake",
    "namespace": "bravo-rehearsal-control",
    "service_account": "rehearsal-host",
    "tenant_id": "registered-tenant",
    "key_id": "reviewed-public-key"
  },
  "public_jwks_file": "/approval/workload-public-jwks.json",
  "operator_verifier": {
    "issuer": "https://confirmed-human-issuer.invalid",
    "audience": "bravo-platform",
    "client_id": "console",
    "assurance": "interactive-mfa",
    "subjects": {},
    "public_jwks": {"keys": []}
  },
  "records_root": "/runner-records",
  "record_key_file": "/runner-signing/key"
}
```

이 예시는 운영 IdP·키·사람 등록이 비어 있어 그대로는 시작에 실패한다.
운영자 계정이나 권한을 임의 생성하지 않는다. DB는 기존 `/credential/username`,
`/credential/password`, `/ca/ca.crt` mount와 `platform-db-rw.platform-state.svc`를
사용한다. schema version 3과 TLS 검증이 필요하다. 기존 window 수신기의
Deployment·자격을 이 호스트로 전환하는 절차가 아니다.
`trusted-host`는 승인 호스트 전용 extra다. 기존 window 이미지의 `postgres`
전용 export에는 T15 로컬 패키지를 포함하지 않는다.

| mount | 쓰기·보유 경계 |
|---|---|
| `/runner-records` | T15 기록의 영속 볼륨. 이 호스트만 쓰고 모델에 mount하지 않음 |
| `/runner-signing/key` | 별도 HMAC Secret. JWT 키와 분리하고 이 호스트만 읽음 |
| `/approval` | 검토한 설정·공개 키. 읽기 전용 |
| `/var/run/secrets/t10-intake/token` | [전용 projected token](../intake/README.md). 전달할 때마다 다시 읽음 |
| `/tls`, `/credential`, `/ca` | 기존 운영 발급/인수 절차의 TLS·DB 입력. 값은 로그에 남기지 않음 |

서버는 TLS 8443, worker 1개로 실행한다. 수평 복제·동일 볼륨 다중 작성자를
설정하지 않는다. `AttemptStore`의 단독 작성자와 T11 Coordinator의 프로세스
수명이 유지돼야 한다. 아직 운영 catalog가 없으면 배포 모듈은 완료되지 않은
상태이며, 이 entrypoint만으로 제품 경로가 열렸다고 보지 않는다.

## 요청 밖에서 실행하고 같은 결과 읽기

`build_trusted_host`는 기존 `build_bucket_host`에 단일 dispatch 작업자를 넣는다.
실행 1개와 대기 1개까지만 받는다. 용량 초과·프로세스 종료·결과 미확인 시
기존 영속 claim을 유지하며 자동으로 다시 제출하지 않는다.
종료 시 대기 작업은 취소하고 실행 중 작업의 종료를 기다린다.

후보 전달과 `ChangeIntake.read_original`은 같은 `RecordedEvidence`와 기록
저장소를 사용한다. 네트워크로 PASS를 복사하거나 별도 T10 Pod에 HMAC 키를
복제하지 않는다. 2·3회차는 T15의 기존 Coordinator와 hold 조건을 그대로 따른다.
프로세스가 사라져 Coordinator를 찾을 수 없으면 기존 T15 경로가 거부한다.

## 취소·보안 hold

관리자는 기존 `OperatorAccess.set_permissions`의 검증된 관리 경로에서 보안·감사
담당에게 계획별 `read`와 `hold_rehearsal`을 부여한다. 일반 변경 승인이나
`start_rehearsal` 권한만으로는 hold를 수행하지 못한다. API에는 권한 부여나
hold endpoint를 추가하지 않는다.

trusted 관리 호출은 `host.intake.hold(human_access_token, plan_id, verification_id,
decision_ref=decision_ref)`를 사용한다. 실제 운영자 토큰, 현재 계획 권한과
유효기간을 계획 잠금 안에서 다시 확인한다. 결정 참조에는 인증된 사람과 대상
검증 ID를 함께 기록한다. 토큰은 저장하지 않는다. 같은 결정 참조를 다른 검증
ID에 재사용하면 거부하고, 권한 회수 뒤에는 이전 호출도 거부한다.

다음 시도 요청과 이전 FAIL의 hold를 자동 결합하는 안은 이 변경에 포함하지
않는다. 취소·보안 hold의 권한과 별도로 합의·검증한다.

## 완료를 구분하기

[T10 이슈 기준](../../../../docs/tasks.md)은 목업 `COLLECT → DONE`과 조건 거부다.
승인·실행 결속 시험은 그 기준을 확인한다. 실제 운영 배포·제품 검증·T31 반영·
전체 컷오버는 다른 인수 결과로 기록한다. TLS entrypoint와 host 조립 시험도
실제 클러스터·모델·사람 로그인 성공을 뜻하지 않는다.

| 이슈의 남은 기준 | 현재 검증 |
|---|---|
| 2번: S2 변경·S3 컷오버 승인 | `tests/test_change.py`, `tests/test_cutover.py`, `tests/test_operator_access.py`: 실제 SQLite/PostgreSQL 저장·멱등·권한과 합성 실행/게이트 입력 |
| 3번: 현재 계획·diff·digest·만료 불일치 거부 | 위 승인 시험과 `tests/test_change_intake.py`: 바뀐 원문·현재 revision·만료·회수·상태 불일치 거부 |
| 완료: 목업 COLLECT부터 DONE | `tests/test_workflow_recovery.py::test_kill_and_resume_from_plan_id`: 각 단계 재시작 및 원격 효과 재조회. 불명확하면 SAFE_HOLD, 같은 효과는 다시 실행하지 않음 |
| 추가 host 연결 | `tests/test_trusted_rehearsal.py`, `tests/test_approval_server.py`: 실제 서명 기록과 API 조립, 요청 밖 단일 실행, 사람 hold 권한, mount 설정, 인증 없는 요청 거부 |

위 시험은 `services/platform-events`에서 `uv run pytest`로 재현한다.
T15·T31 운영 인수는 이 표의 목업 입력을 실제 원본으로 연결할 때 별도로 확인한다.
