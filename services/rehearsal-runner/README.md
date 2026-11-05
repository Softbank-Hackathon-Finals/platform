# rehearsal-runner

리허설 러너(T15). 지금은 리허설 네임스페이스 템플릿과 격리 시험(T3)만 있다. 결정과 실측 결과는 [ADR-0002](../../docs/adr/0002-rehearsal-egress-allowlist.md) · [ADR-0003](../../docs/adr/0003-rehearsal-namespace-isolation.md).

## 바로 해 보기 (k3d, 이 폴더에서)

```bash
scripts/k3d-up.sh                                   # 시험 클러스터 (Docker 필요)
kubectl --context k3d-bravo-rehearsal apply -f testing/standins.yaml   # SeaweedFS · OpenBao 대역 서버
k3d image import busybox:1.36 -c bravo-rehearsal    # 시험 이미지 미리 넣기
bin/create-and-check rehearsal-demo-1 profiles/k3d.env --context k3d-bravo-rehearsal
testing/mutation-check.sh profiles/k3d.env --context k3d-bravo-rehearsal   # 시험이 뚫림을 잡는지
scripts/k3d-up.sh down
```

`create-and-check` 종료 코드: 0 통과 · 1 격리 실패 · 2 시험 불가. 보고서는 `isolation-report-<ns>-<실행 ID>.json`(또는 `--report`).

## 구성

| 경로 | 내용 |
|---|---|
| `templates/` | 리허설 네임스페이스 템플릿. `${NAMESPACE}`와 목적지 프로필 값을 채운다 |
| `profiles/<환경>.env` | 목적지 프로필: 리허설에서 접속을 허용할 SeaweedFS · OpenBao의 네임스페이스 · Pod 라벨 · 컨테이너 포트 |
| `src/rehearsal_runner/isolation/` | 격리 시험. 시험 ID(P1~P4 대조, N1~N13 격리)는 ADR-0003 표와 같다 |
| `testing/` | k3d 대역 서버, 변이 시험 |

## 검사

```bash
uv sync && uvx ruff check . && uv run pytest
```

## T15 격리 시험 연결

이미 생성한 리허설 네임스페이스를 시험하려면 다음 명령을 사용한다.

```bash
uv run python -m rehearsal_runner.isolation_cli --namespace rehearsal-demo-1 --profile profiles/k3d.env --context k3d-bravo-rehearsal
```

통과 시 namespace UID와 시험 실행 ID를 출력한다. 시험 실패·확인 불가·UID 변경·시험용 자원 정리 미확인은 BLOCK이며 종료 코드 1이다. 정리는 최대 30초 기다린다. 모의 리허설 흐름 시험은 격리 실패·불명·UID 변경·시험 자원 누수 시 모의 Job이 호출되지 않고, 성공 시 시험한 namespace UID가 모의 Job으로 전달되는지 확인한다. 이 모의 흐름은 테스트 코드 안에만 있으며 실제 러너를 연결하지 않는다. 이 결과는 검증 Job 실행이나 승인 권한이 아니다. T38 연결 시 Job과 네임스페이스 정리는 같은 UID에 결속하고, T10 실행 권한을 별도로 확인해야 한다.

## 버킷 읽기 관측 프로토타입

`bucket_task.prepare_bucket_policy_task`는 원본 revision·정책 원문·정책 SHA-256·실행 ID·시험 버킷을 담은 번역 작업 입력을 만든다. 중복 JSON 키나 범위 밖 입력은 거부한다. T11 bucket proposer는 원본 스냅샷과 일치하는 경우에만 기존 Coordinator에 제안을 요청하고 지정된 대상 파일 외의 수정은 거부한다. 실제 모델 실행과 T38 제품 검증에는 아직 연결되지 않았으며 SeaweedFS 정책을 적용하지 않는다.

`bucket_probe.observe_bucket_reads`는 신뢰된 호출자가 제공한 인증·무인증 읽기 어댑터로 같은 시험 객체를 인증 → 무인증 → 인증 순서로 관측한다. 기존 객체의 양성 대조와 본문 SHA-256이 모두 일치해야 무인증 403을 해석할 근거가 생긴다. 이 함수의 결과는 관측값이며 T38 영수증이나 승인 판정이 아니다. 테스트는 모의 어댑터만 사용한다.

실제 연결 전에 T9와 시험 전용 버킷·객체·자격 및 지원 정책 형식을, T38과 독립 검증 Job의 인증된 관측 전달 및 비공개 시험 격리 방식을 확인해야 한다. 정책 번역, 실제 SeaweedFS 시험, 버킷 삭제 명령 차단·감사, 정리 및 승인 결속은 아직 구현하지 않았다. T9 개발용 SeaweedFS 구성은 S3 IAM 모드를 끈 상태이므로 임의의 AWS 정책이 적용된다고 가정하지 않는다.

## 버킷 삭제 요청의 현재 차단 경계

현재 T11은 `rehearsal.exec`를 제공하지 않으므로 요청 자체를 거절하고 `TOOL_NOT_ALLOWED`로 기록한다. 제안 내용의 `delete_bucket`도 T11 정책으로 거부한다. 다음 검사는 실제 T11 도구 계층을 호출하지만 버킷 삭제 API나 클러스터를 호출하지 않는다.

```bash
cd ../agent-worker
uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_delete_boundary.py
```

현재 기록은 도구 미허용 또는 제안 거부라는 일반 사유다. 삭제 시도라고 식별되는 감사 이벤트, 향후 허용될 `rehearsal.exec`의 실행 경계, 감사 로그 저장·콘솔 표시는 확인되지 않았다. 해당 계약은 T11·감사 경로 담당자와 확인해야 한다. 이 검사는 T15 버킷 삭제 차단 데모 인수 증거가 아니다.

## 검증 영수증과 재시도 모의 시험

아래 시험은 실제 T11 `Coordinator`에 모의 provider와 영수증을 전달한다. 세 번의 검증 실패, 실패 후 성공, UNKNOWN·효과 불명·실행 정보 불일치, 실패 증거 불일치와 중복 영수증을 확인한다.

```bash
cd ../agent-worker
uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_retry_flow.py
```

이 시험은 프로세스 메모리에서만 실행한다. T38의 실제 두 번째 이후 검증, 영수증 인증·증거 대조, T10의 실행 잠금과 시도 횟수 영속화 또는 재시작 복구는 증명하지 않는다.

## 승인 결속 모의 시험

T10 이벤트 스키마와 모의 수신기로 diff·이미지 digest가 현재 값과 다른 경우와 검증·정리 미확인 경우를 시험한다. 모의 수신기는 테스트 코드에만 있고 실제 승인 경로가 아니다. 이벤트 스키마 통과는 현재 값 일치나 실행 권한의 증명이 아니다.

```bash
cd ../platform-events
uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_approval_binding.py
```

T10 변경 승인 경로, diff 계산 방식, 검증한 제품 이미지와 승인 digest의 신뢰된 결속은 담당자와 확인이 필요하다. 이 모의 시험은 영수증 진위, 이미지 빌드·실행·정리 또는 실제 승인 전달을 증명하지 않는다.

## 자격증명 코드 수정 작업 입력과 모의 시험

`secret_task.prepare_credential_migration`은 demo-chat의 원본 revision과 `app/db_secret.py`, `app/config.py`, `app/main.py`를 대상으로 `/run/bravo/database/credentials.json` 주입 방식의 변경 작업 입력을 만든다. 기존 자격 캐시 및 인증 실패 후 재조회 유지와 값 비노출을 요구한다. 파일 경로와 JSON 키는 `infra/envs/target-secrets/README.md`의 현재 소비 계약을 따른다.

`tests/test_secret_simulation.py`는 가짜 전달자와 DB를 사용하며 실제 demo-chat 수정안 실행이나 Kubernetes RBAC·OpenBao·ESO·DB 연결의 증거가 아니다. T11 제안서·스냅샷과 T38 독립 검증 Job, 실제 Secret 전달·접근 거부 및 자격 회전 검증은 후속 통합으로 남는다.

## 정리와 재시작 경계 시험

아래 시험은 T38의 기존 `Kube.cleanup`에 모의 Kubernetes API 응답을 주어 생성 UID 조건, 삭제 후 부재 확인, 다른 UID의 네임스페이스 발견, 삭제·조회 실패와 시간 초과를 확인한다. 기존 Coordinator를 새로 생성하면 시도 이력이 복구되지 않는다는 한계도 확인한다.

```bash
cd ../agent-worker
uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_cleanup_recovery.py ../rehearsal-runner/testing/test_retry_flow.py
```

모의 API 시험은 실제 네임스페이스 삭제·부재나 T38 제품 리허설 연결을 증명하지 않는다. 재시작 후 실행 잠금·시도 수·영수증 복구와 원격 효과 재조회는 T10/T38 계약 및 실제 구현이 필요하다. 확인 불가 상태에서 승인을 전달하지 않아야 한다.

## 버킷 검증 연결 초안

`bucket_verifier.BucketVerifier`는 버킷 작업과 제안의 실행 ID·원본 revision·해시를 확인하고, 검증 대상 버킷과 시험한 네임스페이스 이름·UID를 주입된 검증 Job 경계에 전달한다. Job 구현은 실행 중 같은 UID를 강제하고 자체 시험 대상·자격을 마련해야 한다. 전달 형식은 프로세스 내부의 초안이며 T38 API 계약이 아니다. 반환값은 인증되지 않은 관측값으로만 취급하고 승인으로 승격하지 않는다. 현재 Job은 테스트 모의 구현만 있으며 실제 정책 적용·T38 검증은 제공하지 않는다.

`bucket_proposal_source.BoundBucketProposalSource`는 T11 결과를 재조회해 실행 ID·원본 revision·제안 digest와 단일 정책 파일 경로를 대조한 뒤 수정 내용을 전달한다. 이 조회는 T38 인증이나 실제 정책 적용 권한이 아니다.

`bucket_s3.bucket_readers`는 신뢰된 세션과 시험용 엔드포인트로 서명된 읽기와 서명 없는 읽기를 별도 클라이언트로 만든다. 로컬 HTTP 시험 서버로 요청 서명 여부와 관측을 확인하며, 실제 S3 정책 적용이나 검증 권한을 제공하지 않는다.

## 자격 주입 검증 연결 초안

`credential_verifier.CredentialVerifier`는 실행·원본 revision·제안 digest와 격리 네임스페이스 UID를 주입된 검증 Job 경계에 결속한다. `credential_probe.observe_credential_delivery`는 Secret 존재 확인 전후로 앱 DB 연결과 다른 서비스 계정의 접근 여부를 관측하며, 오류나 모호한 응답은 `unknown`이다. Secret 값과 자격증명은 관측 결과나 요청에 담지 않는다. 이 접점은 프로세스 내부 초안이며 T38 API 계약이나 인증된 영수증이 아니다. 현재 실제 DB·OpenBao·ESO·Kubernetes 권한 검증 구현과 T11의 demo-chat 파일 접근은 연결되지 않았다.

## 개인 RKE2 버킷 통합 시험

`testing/rke2_bucket_flow.py`는 개인 시험 VM에서만 사용하는 통합 시험이다. T11 검증기에는 고정된 시험 제안을 주고, T15의 실제 격리 게이트와 별도 정책 적용·읽기 검증 Job을 실행한다. 재현에는 독립 시험용 SeaweedFS 네임스페이스·버킷·Secret 및 그에 대한 신뢰된 운영자 권한이 필요하다. 실서비스 자격이나 데이터를 사용하지 않는다. 결과는 테스트 관측값으로, 실제 모델 제안·T38 인증 영수증·T10 승인 공급자를 대체하지 않는다. 검증 Job의 인증 읽기는 시험용 관리자 자격을 쓰므로 앱 전용 최소 권한 증거도 아니다.

시험 스크립트는 Ubuntu VM의 `/etc/rancher/rke2/rke2.yaml`과 `kubectl`을 사용하며 VM 호스트명이 `bravo-t15-rke2`인 경우에만 실행한다. `bravo_agent`, `boto3`, `rehearsal_runner`를 포함한 Python 환경을 준비한 뒤 VM 안에서 관리자 권한으로 실행한다. 보고서 경로는 기존 파일과 겹치지 않아야 한다.

```bash
python3 testing/rke2_bucket_fixture.py --report /개인경로/fixture.json
python3 testing/rke2_bucket_flow.py --fixture-id <fixture.json의 fixture_id> --report /개인경로/flow.json
```

첫 명령은 임의 자격과 시험 버킷을 생성하고 네임스페이스 수신을 차단한다. 둘째 명령은 공개 대조 후 제한 정책을 복원하며 리허설 네임스페이스와 임시 접근 규칙은 UID 조건으로 정리한다. 시험용 SeaweedFS 네임스페이스와 버킷은 재실행을 위해 남는다. 중단·오류 때는 보고서와 잔여 자원을 먼저 확인한다.

## T10 승인 경계 교차 시험

T15 실제 러너의 미인증 결과·격리 실패·정리 실패를 T10 실제 변경 승인 코드에 전달해 승인 거부를 확인한다. 모의 Job의 PASS는 인증된 제품 검증이나 승인 공급자가 아니다.

```bash
cd ../platform-events
PYTHONPATH=../rehearsal-runner/src uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_t10_bucket_boundary.py
```

## 단일 시도 실행 순서

`rehearsal_runner.runner.run_once`는 주입된 제안·생성·검증·정리 경계와 `T3Gate`를 이용해 한 번의 시도 순서를 관리한다. T3 시험 전 현재 namespace UID를 생성 결과와 대조하고, 시험 후에도 이름·UID가 다르거나 시험이 실패하면 검증을 호출하지 않는다. 생성 결과의 이름은 `rehearsal-` 범위로 제한하고, 정상·실패 경로 모두 원래 UID로 정리를 요청한다. 정리 부재를 확인하지 못하면 사람에게 넘긴다.

이 함수에는 실제 T10 실행 승인·잠금·시도 영속화, T11 제안 어댑터, T38 제품 검증 및 인증된 영수증 검증 어댑터가 연결되어 있지 않다. T38이 같은 UID를 실행 중 강제하는 계약도 필요하다. 반환된 `RECEIPT_UNVERIFIED`의 영수증은 신뢰하지 않은 입력이며 승인 대기가 아니다. 실제 자원에서 실행하거나 자동 재시도·승인에 연결해서는 안 된다. 프로세스 중단 시 정리와 외부 효과의 확인·복구도 별도 계약이 필요하다.


### T11 경계 확인 사항

현재 T11 `safe_path`는 파일명에 `secret`이 포함된 `app/db_secret.py`를 거부한다. 해당 파일은 demo-chat이 Secrets Manager를 직접 호출하는 위치이므로, T15 작업 입력만으로는 현재 T11 스냅샷·제안에 포함할 수 없다. T11 담당자와 파일 내용·민감값 보호를 유지하는 허용 범위 및 제안서 연결을 합의해야 한다. T15가 별도 우회 읽기 경로를 만들지 않는다.


### 네임스페이스 생성과 UID 정리 접점

`namespace_lifecycle.NamespaceLifecycle`은 새 이름의 네임스페이스를 생성해 API가 반환한 UID를 보존하고 T3 템플릿 적용 전후로 UID를 확인한다. 정리는 주입된 UID 조건 cleaner에 원래 이름과 UID를 전달한다. 테스트에서는 T38의 기존 `Kube.cleanup`과 모의 Kubernetes API를 연결해 UID 조건과 부재 확인을 검증한다.

```bash
cd ../agent-worker
PYTHONPATH=../rehearsal-runner/src uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_namespace_cleanup_interop.py
```

아직 실제 T38 검증 Job은 T3가 시험한 namespace·UID를 입력으로 받지 않는다. 생성·시험·Job·정리의 단일 UID 보장과 정리 책임, 공통 클러스터 context, T10 실행 잠금 및 재시작 복구를 담당자와 확정하기 전에는 이 경로를 실제 자원에 연결하지 않는다. 템플릿 적용 전후 UID 조회는 그 사이의 변경을 원자적으로 막지 못한다.


### 버킷 작업과 T11 제안 연결

`bucket_proposer.T11BucketProposer`는 같은 run·revision의 T11 Coordinator와 읽기 전용 snapshot을 사용한다. 원본 정책 내용·해시와 고정된 작업 요약을 대조하고, T11이 검증한 제안이라도 지정된 대상 파일 한 개를 벗어나면 차단한다. 동기 호출이며 2·3회차에는 같은 실행·원본 revision, Coordinator의 `RETRY_ALLOWED` 상태와 외부에서 인증한 실패 증거 digest가 일치해야 한다. 증거 인증과 재시작 후 시도 영속성은 호출 호스트의 책임이다.

```bash
cd ../agent-worker
PYTHONPATH=../rehearsal-runner/src uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_bucket_t11.py
```

이 시험은 실제 T11 Coordinator·도구 정책과 모의 모델·검증기를 사용한다. 리허설 기본 `pytest` 수집에는 포함되지 않는다. 실제 모델·SeaweedFS 정책 적용·T38 검증, UID 결속, 승인 여부는 확인하지 않는다. T38 실행 통로에 제안서를 저장·전달하는 어댑터와 T10 실행 권한·재시도 복구는 별도다.


## 자격 증명 제안과 재시도 접점

`T11CredentialProposer`는 demo-chat 세 소스 경로와 원본 revision을 기존 T11 snapshot·제안 검증에 전달한다. 실제 T11 경로 정책이 `app/db_secret.py`를 막는 동안에는 모델 호출 전에 중단한다. 이 어댑터는 정책을 우회하지 않는다.

재시도 횟수와 이전 결과의 hold 확인은 T10이 담당한다. T15는 접수된 단일 시도만 실행하며 검증 결과 자체를 승인으로 취급하지 않는다.

`credential_proposal_source.BoundCredentialProposalSource`는 제안의 실행·원본 revision·digest와 허용된 demo-chat 파일 및 원본 해시를 대조해 수정안을 검증 Job 경계에 전달한다. 수정 내용은 객체 표현에 출력하지 않는다. 이 조회만으로 수정안·이미지의 안전성이나 실제 실행을 증명하지 않는다.

## 자격 주입 검증 관측 명령

`python -m rehearsal_runner.credential_job_probe secret-get --namespace <namespace> --name <secret>`은 실행된 Pod의 서비스 계정으로 Kubernetes API의 Secret GET 상태만 확인한다. 응답 본문과 토큰은 읽어 출력하지 않으며, 200은 `yes`, 403은 `no`, 나머지는 `unknown`이다. `app-db --namespace <namespace> --service <service> --port <port>`는 demo-chat DB health 응답을 확인한다. 각 명령은 관측값만 출력하며 T38 영수증이 아니다.

리허설 기본 NetworkPolicy는 Kubernetes API 송신을 막고 기본 계정에 토큰을 마운트하지 않는다. 이 명령을 실행하려고 격리 정책을 임의로 완화하지 않는다. 신뢰된 별도 검증 Job, 대상 계정의 식별·권한 전달, 양성 대조, 인증된 Job 증거와 UID 정리 방식은 T38 접점에 맞춰 연결해야 한다. 실제 앱의 DB 응답과 대상 Secret에 대한 별도 양성·음성 확인이 모두 필요하다.

로컬에서 실제 수정된 demo-chat과 PostgreSQL 연결을 재현하려면 수정본의 `.venv`와 PostgreSQL 도구(`pg_config`, `initdb`, `pg_ctl`, `psql`)를 준비한 뒤 `testing/verify_real_demo_db.sh <수정된-demo-chat-경로>`를 실행한다. 이 시험은 임시 DB와 자격을 만들고 초기 접속, 비밀번호 회전 후 재접속, DB 중단 관측을 확인한 뒤 정리한다. Kubernetes·OpenBao·ESO·T38 제품 Job 검증을 대신하지 않는다.

## T10 수신 교차 시험

`approval_handoff.submit_candidate`는 호스트가 주입한 T10 `Candidate`, `ChangeIntake`, 토큰 공급자를 이용해 후보를 한 번 전달한다. T10의 응답을 검증하며 예외·응답 불명일 때 자동으로 재전송하거나 승인되었다고 표시하지 않는다. 인증 원문 조회와 수신자 권한 검증은 T10 책임이다.

머지된 T10 수신 PR #237의 실제 코드와 합성 원문·시험용 서명 키로 다음 교차 시험을 실행한다.

```bash
cd ../platform-events
PYTHONPATH=../rehearsal-runner/src uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_t10_intake_compat.py
```

PR #237의 수신기는 기본적으로 네트워크 경로와 운영 원문 조회기가 설치되지 않으며, 이 시험은 실제 제품 검증이나 사람 승인을 뜻하지 않는다.

재시도는 T10 수신 계약에 맞춰 이전 검증 ID의 hold 커밋을 확인한 다음에만 시작한다. `T10PreviousHold`에는 T10 관리 호스트가 승인한 hold 권한과 intake를 주입한다. 응답·권한·저장 확인이 불명확하면 다음 시도를 시작하지 않는다. T15 워크로드나 모델에 hold 권한을 부여하는 설정은 이 코드에 없다.

두 제안 어댑터와 제안 조회 경계는 시도 번호를 일치시킨다. 재시도 digest 비교만으로 실패 증거의 출처가 인증되는 것은 아니며, 실제 T38 인증 결과와 T10 실행 권한·영속 복구 연결이 필요하다.

## T10 재시작 복구 조회

`NamespaceLifecycle`은 T10이 발급한 실행 ID와 시도 번호의 조회용 라벨·annotation을 새 네임스페이스에 설정하고 템플릿 적용 전후에 원 UID와 식별값을 확인한다. `recovery_lookup.find_live_namespace`와 `find_live_jobs`는 같은 실행·시도의 현재 namespace·Job 후보를 조회하며, Job 목록 조회 전후의 namespace UID도 대조한다. 중복·삭제 중·식별값 불일치는 조회 불명으로 처리한다.

라벨은 검색 용도다. 조회 후보는 인증된 Job·Pod·이미지·로그 증거나 원 UID의 삭제·부재 증거가 아니다. namespace가 보이지 않는 것만으로 생성되지 않았거나 정리됐다고 판단하지 않는다. T38의 인증된 실행 원문과 보존 경로를 연결하기 전 T10 `Observation`으로 변환하지 않는다. T38 Job에도 동일 실행·시도 식별자를 붙이는 접점은 담당자와 협의 중이다.

```bash
cd ../platform-events
PYTHONPATH=../rehearsal-runner/src uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_t10_recovery_lookup.py
```

위 교차 시험은 실제 T10 접수·재조회 코드와 모의 Kubernetes API를 사용한다. 제출 응답 유실 후 중복 제출 없이 기존 Job UID를 발견하지만, 인증된 결과가 없으므로 `UNCONFIRMED`가 유지되고 다음 게이트가 차단됨을 확인한다.

T10 관리 방식의 실행은 `admission.run_admitted_attempt`가 접수된 단일 시도만 실행한다. T10의 `Admission.rehearsal_id`·시도·원본 revision·작업 digest와 T15 작업 및 Job 시도를 대조하고, 불일치하면 namespace를 만들지 않는다. 논리 작업 digest는 실행 ID를 제외해 재시도 사이에 유지하며 버킷 원본·대상 또는 자격 이전 요구사항은 포함한다. T10이 각 시도를 계획 잠금에서 접수하고 이전 hold를 확인하며, T15가 직접 다음 시도를 접수하지 않는다. 호출 호스트는 접수 원문의 신뢰성, 원격 제출 중복 방지와 재시작 시 효과 재조회를 제공해야 한다.

```bash
cd ../platform-events
PYTHONPATH=../rehearsal-runner/src uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_t10_rehearsal_host.py
```

`rehearsal_host.T15RehearsalHost`는 T10 `RehearsalHost`의 `task`·`submit`·`observe`에 맞춘 어댑터다. 계획에 등록된 작업을 읽어 digest를 만들고, 접수 커밋 이후 동일 작업인지 다시 비교한 다음 T15 제출자에게 전달한다. 인증된 T38 원문과 같은 `Admission`·생성/삭제 UID·부재 확인이 없는 경우 T10 결과를 반환하지 않는다. 제출자와 결과 조회기는 아래 `attempt_store`를 주입한다. 작업 저장소와 운영 배치는 T10 운영 호스트가 정한다.

```bash
cd ../platform-events
PYTHONPATH=../rehearsal-runner/src uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_t10_rehearsal_host.py
```

`Dockerfile.credential-probe`는 위 관측 명령만 포함하는 별도 검증자 이미지를 정의한다. 실제로 검증할 demo-chat 후보 이미지와 다른 이미지이며, 승인에 결속할 digest는 후보 이미지의 digest다. 고정된 기반 이미지·비루트 사용자·격리 Python 실행을 사용한다. 이미지 빌드·스캔·T38 Job 배포·서비스 계정·Kubernetes API 접근은 별도 검증이 필요하다.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q testing/test_credential_probe_runtime.py
```

이 시험은 컨테이너 빌드를 대신하지 않으며, 필요한 모듈만 설치된 격리 Python에서 명령이 실행되고 외부 `PYTHONPATH`를 무시하는지만 확인한다.

## 시도 기록과 버킷 검증 Job

`attempt_store.RecordedAttemptSender`는 T10이 커밋한 시도를 `claim.json`으로 한 번만 점유하고 실행한 뒤 결과를 `result.json`에 한 번만 쓴다. 기록은 러너 호스트만 가진 키로 HMAC 서명한다. 점유 뒤 결과가 없으면 T10에서 `UNCONFIRMED`로 남고 다시 실행하지 않는다. PASS 기록은 `deliver`로 T10 `ChangeIntake`에 한 번 전달한다. `RecordedEvidence`는 T10 `EvidenceReader`와 `ChangeIntake.read_original`을 같은 서명 기록에서 제공한다. 2·3회차는 직전 FAIL 기록의 증거 digest를 T11 재시도 근거로 받는다. 환경 원인의 실패는 `NEEDS_HUMAN`과 고정 원인 코드(`detail`)로 남는다.

`bucket_job.SeaweedBucketJob`은 T3가 시험한 namespace에서 원본 정책(대조군)과 후보 정책을 차례로 적용하는 Job 두 개를 실행한다. Job은 토큰 없는 기본 계정으로 실행한다. 각 Job은 인증 읽기·무인증 읽기·인증 읽기를 관측하고, T38 `observe_job_pod`로 Job·Pod·컨테이너·imageID를 결속한다. 대조군에서 무인증 읽기 허용이 관측되지 않으면 판정하지 않는다. 후보가 무인증 읽기를 막고 인증 읽기를 유지하면 PASS, 정책 거부·무인증 허용·인증 읽기 손상은 FAIL이다. 시험 버킷·자격·수신 허용은 호스트가 제공한다.

```bash
cd ../platform-events
PYTHONPATH=../rehearsal-runner/src uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_t10_recorded_flow.py
```

`testing/rke2_rehearsal_flow.py`는 개인 RKE2 시험 VM 전용이다. `setup`은 시험용 SeaweedFS·OpenBao 대역·버킷을 만들고, `run`은 실제 T10 접수·수신·변경 승인(SQLite), T11 Coordinator, T3 격리, 검증 Job과 UID 정리를 연결한다. 제안은 스크립트로 고정하며 모델·팀 목적지·사람 승인을 대신하지 않는다.
