# T7 플랫폼 DB와 환경 접근 역할

기존 RKE2의 독립 플랫폼 DB와 T10 소비 검증, collector/Executor Kubernetes 권한 분리다. [ADR-0012](../../../docs/adr/0012-platform-data-access-boundary.md)의 개발 배치 제안이며 전체 T7·T10·T12 완료가 아니다.

## 소유 범위

| 코드 | 소유·입력 | 결과 |
|---|---|---|
| Terraform | 기존 인스턴스/VPC는 조회, 별도 remote state `runtime/platform-data/terraform.tfstate` | 암호화 16GiB gp3와 attachment만 생성 |
| `mount_volume.py` | private EBS ID, 정확한 디스크/파일시스템 검사 | 별도 mount와 다음 RKE2 부팅의 mount 의존성 |
| `render.py` · `apply_host.py` | 현재 노드명, T10의 검토한 SQL | CNPG·PV·namespace·NetworkPolicy·분리된 DB Secret |
| `access_roles.py` | 환경 namespace, 정확한 PgBouncer ConfigMap 이름 | collector/Executor ServiceAccount·Role·RoleBinding |
| `probe_job.py` · `verify_t10.py` | T10 source와 잠금 의존성 | 클러스터 안에서 합성 상태/HTTP/SSE·권한 거부 검증 |
| `verify_access.py` · `verify_platform_host.py` | 운영자 인증으로 만든 10분 테스트 토큰 | 실제 API 거부·정확한 ConfigMap patch·DB 네트워크 차단 |
| `verify_runtime_path.py` | 임시 검증 정책이 없는 상태 | 상시 DB/DNS 경로로 새 psql TLS·T10 테이블 읽기 확인 |
| `cleanup_probes.py` | 명시한 소유 Job 목록 | 종료된 Job·사용이 끝난 임시 정책만 정리, bundle 보존 |
| `operate.py` | 기존 운영자 AWS 로그인, private 대상 계약·receipt 경로 | 계정/태그/VPC/사설 노드 대조 후 고정 스크립트 SSM 실행·조회 |

기반 EC2·VPC·IAM·OIDC·registry state와 T6 업무 DB는 이 스택에서 변경하지 않는다. DB root/CA 개인키와 비밀번호를 읽어 로컬에 가져오는 경로가 없다. 운영자 설치 도구는 제품 에이전트 도구 allowlist에 넣지 않는다.

## 배치 순서

1. 소유 세션 인계, GitHub T7 상태 `In progress`, 현재 본인 AWS 신원을 확인한다. backend/config/receipts는 checkout 밖 owner-only 경로에 둔다. AWS config에는 `expected_account`, `region`, `instance_id`, `vpc_id`만 필요하다. 장기 키를 새로 만들지 않는다.
2. `terraform init -backend-config=...`, `plan -var-file=... -out=...`을 실행한다. plan의 managed 변경이 EBS와 attachment 생성 2개뿐인지 검토한 뒤 저장한 plan을 apply한다. `account_id`, `instance_id`, `vpc_id`는 private tfvars로 전달한다. 기반 state를 재사용하지 않는다.
3. `operate.py submit --phase mount`에 `{"volume_id":"<reviewed-volume>","initialize_empty_volume":true}`를 준다. 재시도는 새 operation 이름을 쓰기 전에 기존 receipt를 poll한다. 이미 ext4면 초기화하지 않고 UUID/소유권을 대조한다. 알 수 없는 디스크·서명·mount·기존 파일은 중단한다.
4. 현재 정본의 T10 SQL로 `render.py --node ... --consumer-root ...`를 실행한다. `manifest`, `cnpg_values`(동봉 YAML 내용), `apply_database:false`를 포함한 JSON으로 `--phase database`를 실행한다. HelmChart가 고정 chart 0.29.1/operator digest를 설치한다. 비밀번호는 해당 호스트 안에서만 생성한다.
5. operator가 준비된 뒤 같은 입력의 `apply_database:true`로 실행한다. 서버 dry run 후 실제 배치한다. 첫 초기화에는 T10 SQL이 적용되며, 기존 데이터의 재배치에서 bootstrap SQL은 migration으로 다시 실행되지 않는다. 후속 schema 변경은 별도 migration 절차로 검토한다.
6. DB Ready와 별도 PV Bound를 확인한다. 현재 DB/Secret 참조와 T10 source/migration hash를 private 소비 계약으로 보존한다. 자동 SQLite migration이나 T33 테이블 공유는 하지 않는다.

실행 예시(경로와 대상 값은 본인 private 계약으로 제공):

```sh
python3 infra/envs/platform-data/operate.py submit --phase database --name db-apply-1 \
  --config "$OPERATOR_CONFIG" --payload "$DB_INPUT" --receipts "$PRIVATE_RECEIPTS"
python3 infra/envs/platform-data/operate.py poll --name db-apply-1 \
  --config "$OPERATOR_CONFIG" --receipts "$PRIVATE_RECEIPTS"
```

출발지/목적지 접근 역할은 각 환경 소유자와 namespace/ConfigMap을 확인한 뒤 렌더·서버 dry run·apply한다. 두 ServiceAccount가 자동으로 플랫폼 Pod의 원격 인증이 되는 것은 아니다. TokenRequest 발급/회전 경로와 대상 연결은 소비자와 별도로 구성한다. 이번 원격 실측은 출발지 EKS이며 목적지 실측은 아직 없다.

## T10 소비 검증

현재 `services/platform-events`의 `postgres` extra와 SQL을 사용한다. 배치한 스키마의 migration SHA와 코드 commit을 함께 기록한다. 이번 실측 소비자는 #94의 `4d670fb565fba2fde9d7c7eb84776c5a7f211def`이며 develop 병합 `7449745`의 같은 구현이다.

```sh
uv export --project services/platform-events --frozen --extra postgres \
  --no-emit-project --format requirements-txt --output-file "$PROBE_REQUIREMENTS"
python3 infra/envs/platform-data/probe_job.py --consumer services/platform-events \
  --requirements "$PROBE_REQUIREMENTS" --name t7-db-initial --probe-id t7-state-example > "$PROBE_MANIFEST"
```

Job은 고정 Python image와 hash-pinned wheel을 사용한다. 코드/요구사항은 immutable ConfigMap의 해시 이름에 묶이고, 비루트·read-only root·tokenless로 실행한다. runtime Secret과 CA의 공개 인증서만 mount한다. `probe_job` manifest를 `{"action":"apply","name":"...","manifest":...}`로 감싸 `operate --phase probe`로 배치한다. `action:read`는 JSON 검증 결과만 읽는다. 설치 로그·DB 오류 상세·자격 값은 내보내지 않는다.

같은 합성 probe ID로 첫 Job과 DB Pod 재생성 뒤의 별도 Job을 실행해 snapshot/receipt 해시를 대조한다. `action:restart_database`는 정확한 `platform-state/platform-db-1`의 owner label을 확인하고 이 Pod만 재생성한다. 기본적인 저장소 객체 재개와 이 실제 Pod/프로세스 재기동 시험을 구별한다. 이 경로는 기존 업무 앱을 전환하지 않는다.

검증 뒤에는 `operate --phase cleanup`에 명시한 완료 Job 목록(`{"jobs":["t7-db-initial","t7-db-recovery"]}`)만 제거한다. 다른 T7 probe가 실행 중이면 임시 `t7-verification` 정책도 보존한다. immutable ConfigMap은 다른 Pod/컨트롤러가 재사용할 수 있으므로 보존한다. `operate --phase runtime-path`로 임시 정책 부재와 상시 경로의 새 TLS 연결을 확인한다. 이 검사는 전체 T10 소비 시험과 별도로 기록한다. DB·Secret·PVC·볼륨·source 역할은 개발 연결을 위해 유지한다. cleanup 전에 정확한 Job 참조와 이름을 확인하며 namespace/PVC/DB를 삭제하지 않는다.

## 운영 제한

- 단일 인스턴스·단일 노드·단일 AZ다. Pod 재생성은 HA·볼륨 손실·off-host 복원 시험이 아니다. PV Retain과 EBS 보존도 백업을 대신하지 않는다.
- T10 runtime은 신뢰된 플랫폼 호스트 자격이다. SQL role 자체를 tenant별 보안 경계로 사용하지 않고 서버의 등록/인가 계약을 유지한다. 합성 ASGI principal을 외부 서비스에 사용하지 않는다.
- collector/Executor에는 DB 자격이 없고 DB namespace·네트워크도 거부된다. 실제 T12 SQL 제어와 승인·epoch·명령 결속, 목적지 연결은 후속이다.
- 추가 상시 자원은 gp3 16GiB 한 개다. 실제 청구액과 별도 snapshot/백업 비용은 비용 조회에서 확인한다. 기존 월 50만원 예산 경계를 유지한다.
- 상태/명령 영수증은 감사 조회 재료이며 S3 Object Lock 불변 감사 저장소가 아니다. T33 데이터 이식과 T24 수집은 별도다.

## 로컬 검사

```sh
python3 -m unittest scripts.tests.test_platform_data -v
uvx ruff check infra/envs/platform-data scripts/tests/test_platform_data.py
terraform fmt -check -recursive
terraform -chdir=infra/envs/platform-data init -backend=false
terraform -chdir=infra/envs/platform-data validate
terraform -chdir=infra/envs/platform-data test
uv run --project services/platform-events --extra postgres pytest services/platform-events/tests -q
```

[실제 검증 기록](../../../docs/evidence/t7-platform-data-2026-11-03/README.md)을 PR에서 함께 검토한다. 준비된 PR은 `In review`, 수정·추가 구현 중이면 `In progress`로 보드를 유지하며 전체 인수 전 Done 처리하지 않는다.
