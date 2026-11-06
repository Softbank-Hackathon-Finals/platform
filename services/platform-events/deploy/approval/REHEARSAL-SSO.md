# 기존 AWS SSO로 리허설 시작·결과 조회

이 호스트는 등록된 계획에서 첫 리허설을 시작하고 같은 결과를 조회한다.
등록용 SSO 증명과는 서명 audience, 주체 ID, Python 타입을 분리한다.
사람의 변경 승인·컷오버 승인·배포 권한을 만들지 않는다.

## 인증과 현재 권한

운영자 클라이언트는 기존 SSO 프로필로 다음 함수를 호출한다.

```python
from bravo_platform_events.rehearsal_sso import make_rehearsal_token

proof = make_rehearsal_token(profile_name=explicit_profile, region=observed_region)
```

호출자는 proof를 메모리에서 HTTPS Bearer 헤더에 전달한다. 파일·argv·로그에는
저장하지 않는다. SDK 클라이언트에는 기존 `sso-registration` 선택 의존성을 사용한다.
서버는 기존 STS 검증 구현으로 정확한 계정·불변 SSO RoleId·세션 ARN을 확인한다.
서명 audience는 `bravo-platform-rehearsal`이다. 최대 60초 수명과 SDK 진단 로그
거부는 기존 검증기에 따른다. 이 검증은 MFA나 SSO 로그인 시점을 증명하지 않는다.

반환 주체는 `SsoRehearsalPrincipal`이다. `OperatorPrincipal`과
`SsoRegistrationPrincipal`이 아니므로 기존 승인·등록 경로에서 거부한다.
주체 ID는 `rehearsal_subject(registration_subject(...))`로 계산한다.
계산된 ID 자체는 권한이 아니다.

배포 담당은 사용자 실행 지시와 실제 등록 영수증을 확인한 후, 별도 보호 mount에
정확한 주체·tenant·plan·기간·관리 결정 참조를 기록한다. HTTP로 grant를 만들 수 없다.

```json
{
  "version": 1,
  "grants": [{
    "subject_id": "rehearsal-confirmed-subject",
    "tenant_id": "registered-tenant",
    "plan_id": "registered-plan",
    "valid_after": "2026-11-05T13:00:00Z",
    "expires_at": "2026-11-05T14:00:00Z",
    "administration_ref": "accepted-rehearsal-window"
  }]
}
```

예시 값은 운영 입력이 아니다. grant는 start·reconcile·snapshot만 허용한다.
항목을 제거하면 회수된다. 파일 오류·중복 주체/계획·다른 tenant·만료는 거부한다.
기존 요청 영수증을 반환하기 전에도 계획 잠금 안에서 파일과 인증 만료를 확인한다.
배포 담당은 보호 파일의 revision/hash와 관리 결정 원문을 실행 기록에 남긴다.

## 호스트 조립

```sh
uv sync --locked --no-dev --extra postgres --group product-inputs
python -m bravo_platform_events.rehearsal_server \
  --config /rehearsal/host.json --ports reviewed_deployment:create_ports \
  --tls-cert /tls/tls.crt --tls-key /tls/tls.key
```

`create_ports(store)`는 실제 `RehearsalPorts(catalog, planner)`를 반환한다.
호스트는 기존 `build_bucket_host`, `AttemptStore`, `ChangeIntake`를 연결한다.
T15가 기록한 같은 서명 저장소로 결과 전달과 원본 조회를 수행한다.
모델·Kubernetes·검증 판정의 기본 성공값이나 대체 구현은 제공하지 않는다.

`host.json`의 정확한 필드는 다음과 같다.

| 필드 | 운영 입력 |
|---|---|
| `version` | 정수 1 |
| `workload_binding`, `public_jwks_file` | 기존 [T15→T10 projected token 계약](../intake/README.md)의 실제 SA/issuer/audience/tenant/검토한 공개 키 |
| `sso_verifier` | 기존 등록 STS 검증기의 계정·RoleId·세션·region·tenant. audience만 `bravo-platform-rehearsal` |
| `grants_file` | 위 grant의 읽기 전용 절대 경로 |
| `records_root`, `record_key_file` | 같은 T15 단독 작성자의 영속 `AttemptStore`와 전용 서명 키. TaskCatalog 키와 별도 |

SSO tenant와 workload tenant는 같아야 한다. 실제 DB 자격/CA mount와 schema 3을
재사용하며 migration이나 등록 행을 생성하지 않는다. T15 결과 전달의 projected
token은 매번 다시 읽는다. 서버에 사람의 SSO 캐시·운영자 gh·복호 개인키를 넣지 않는다.

서버는 TLS 8443, worker 1개다. 기존 제한 dispatcher로 요청 밖에서 한 작업만
실행한다. 종료 시 진행 중인 작업의 종료를 기다린다. 여러 호스트가 같은 기록
볼륨에 쓰면 안 된다. 승인 호스트와 동시에 같은 계획을 실행하지 않는다.

## 요청과 완료 범위

- `POST /v1/plans/{plan_id}/rehearsals`: 기존 `task_id`, `expected_version`,
  `attempt=1`, `Idempotency-Key`를 사용한다. 같은 요청은 기존 영수증을 읽는다.
- `POST /v1/plans/{plan_id}/rehearsals/{run}/attempts/1/reconcile`: 같은 시도의
  인증된 결과와 UID 정리를 확인한다. 새 workflow나 모델을 발행하지 않는다.
- `GET /v1/plans/{plan_id}`: 현재 권한을 읽기 전후로 검사해 snapshot을 반환한다.

등록·grant·hold·승인·실제 배포 경로는 없다. T38 제품 경로의 2·3회차는 열지 않는다.
이 호스트는 계획을 임의로 `REHEARSE` 상태로 바꾸지 않는다. 실제 수집·분석·계획
원본에 따른 기존 전이가 먼저 필요하다. 시작 실패·발행 확인 불가는 기존 영속
claim을 유지하며 자동 재제출하지 않는다.

완료 근거는 동일한 실제 plan/Admission/attempt의 T38 입력·원격 결과, T15 검증 Job과
UID 정리, T10 현재 후보 반영이다. SSO 인증·호스트 시작·로컬 합성 시험만으로
전체 E2E나 사람 승인 완료를 보고하지 않는다. T15 참조 전용 소비 포트, 실제
catalog/입력/current grant, 대상 profile/자격, 전용 mailbox 및 실행창을 인수해야 한다.

## 저장된 최초 제안을 쓰는 자격 이전 호스트

`--ports bravo_platform_events.credential_host:create_ports`는 `/rehearsal/ports.json`의
보호된 설치 입력으로 실제 `StoredProposalSource` → `CredentialPlanner` → T7
`ScopedPlanner`를 연결한다. 기존 정본 Admission을 조회한 뒤에만 같은 입력·제안·
발행 이미지가 소비된다. 원 최초 제안의 현재 목적 권한·만료·서명 catalog·publication·
workflow checkpoint를 다시 검사한다. 이 호스트에는 모델 제출 기능이 없다.

설치 입력은 환경 등록 시 한 번 준비하며 HTTP 요청이나 모델이 선택하지 않는다.
기존 `host.json`의 권한과 서명 기록 설정은 그대로 사용한다.

| 필드 | 기존 운영 입력 |
|---|---|
| `reader_config` | 제품 읽기 서비스의 catalog·GitHub 공개 키 설정 파일 |
| `runtime_source_sha` | 해당 최초 실행에 고정된 검토·설치 소스 SHA |
| `target_environment_id`, `cluster_uid` | 등록된 목적지와 실제 클러스터 UID |
| `manager_subject` | 등록된 별도 관리 SA의 전체 주체명. t15-runner와 달라야 함 |
| `manager_kubeconfig`, `runner_kubeconfig` | T7가 공급하는 갱신 가능한 별도 kubeconfig. 같은 TLS 경로·클러스터 구성 필수 |
| `profile_file` | 기존 T3 목적지 `.env` 프로필 |
| `isolation_image` | digest로 고정한 T3 검증 이미지 |
| `t9_renderer`, `scoped_lifecycle_module` | 같은 검토 소스의 T9 렌더러·T7 ScopedPlanner 모듈 |
| `fixture` | `bao_image`, `postgres_image`, `probe_image`, `openbao_ca_file`. 기존 CredentialFixture 입력 |
| `candidate_repository` | 검증된 발행 이미지를 읽는 저장소. digest는 같은 계획의 발행 영수증에서만 읽음 |
| `staging` | 호스트 전용 임시 소스 디렉터리 |

기동 시 두 클라이언트의 실제 신원, 동일 TLS 연결 설정, 관리 클라이언트가 읽은
클러스터 UID를 대조한다. 실행 시 계획 목적지도 검사한다. 관리 클라이언트는
namespace/T3/UID 정리에만, scoped runner는 검증 Job에만 사용한다. 이미지 조회는
T15가 계산한 diff와 현재 계획의 발행 영수증이 같을 때만 허용한다. 보호 mount가
없거나 자격 발급·클러스터 확인에 실패하면 시작하지 않는다. 설치가 권한·계획·
발행 영수증·성공 판정을 생성하지는 않는다.

배포 이미지는 기존 제품 이미지 빌더의 `--rehearsal-host` 모드로 만든다.
커밋된 소스와 잠긴 Python 의존성만 포함하고, T7/T9 모듈과 공식 SHA256에 고정한
kubectl v1.35.9를 추가한다. 빌더는 UID 10001·읽기 전용·네트워크 없음 조건에서
호스트 import와 kubectl 기동을 확인한다. 이미지 생성은 배포나 실행을 시작하지 않는다.

```sh
uv run --project services/platform-events python \
  services/platform-events/deploy/product-read/build.py \
  --rehearsal-host --output /private/new-host-build
```
