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

`bucket_proposer.T11BucketProposer`는 같은 run·revision의 T11 Coordinator와 읽기 전용 snapshot을 사용한다. 원본 정책 내용·해시와 고정된 작업 요약을 대조하고, T11이 검증한 제안이라도 지정된 대상 파일 한 개를 벗어나면 차단한다. 현재 최초 시도만 지원하며 동기 호출이다.

```bash
cd ../agent-worker
PYTHONPATH=../rehearsal-runner/src uv run --frozen --no-default-groups --with pytest pytest -p no:cacheprovider ../rehearsal-runner/testing/test_bucket_t11.py
```

이 시험은 실제 T11 Coordinator·도구 정책과 모의 모델·검증기를 사용한다. 리허설 기본 `pytest` 수집에는 포함되지 않는다. 실제 모델·SeaweedFS 정책 적용·T38 검증, UID 결속, 승인 여부는 확인하지 않는다. T38 실행 통로에 제안서를 저장·전달하는 어댑터와 T10 실행 권한·재시도 복구는 별도다.


## 자격 증명 제안과 재시도 접점

`T11CredentialProposer`는 demo-chat 세 소스 경로와 원본 revision을 기존 T11 snapshot·제안 검증에 전달한다. 실제 T11 경로 정책이 `app/db_secret.py`를 막는 동안에는 모델 호출 전에 중단한다. 이 어댑터는 정책을 우회하지 않는다.

`retry.run_retries`는 시도별 T3 시험과 UID 조건 정리를 완료한 후 신뢰된 결과 판정 포트가 같은 실행·제안·시도의 재시도를 허용한 경우에만 다음 시도를 시작하며 최대 3회다. 결과 불명, 정리 미확인, 결과 불일치는 사람에게 넘긴다. 모든 의존성은 주입된 포트이며, 현재 영수증 인증기·T11 후속 시도 어댑터·T10 실행 잠금과 영속 복구는 설치되지 않았다. 메모리 시뮬레이션을 실서비스 승인 또는 안전한 재시작으로 취급하지 않는다.
