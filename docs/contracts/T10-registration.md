# 환경, 앱, 이전 계획 등록

운영자는 읽기 주체로 환경을 확인하고, 실행 중인 앱을 검토한 코드와 이미지에 연결한 뒤 이전 계획을 등록한다. P0 환경 쌍은 AWS EKS 출발지와 온프렘 RKE2 목적지다. [설계 §8.9·8.11](../design.html#s8-11) · [T10 계획 저장 계약](T10-plan-snapshot-v1-proposal.md)

| POST | 요청 본문 | 최초 응답 |
|---|---|---|
| `/v1/environments` | `name`, `kind`(`aws` 또는 `onprem`), `roles`([`source`] 또는 [`target`]), `connection_ref` | 서버가 부여한 `environment_id`, 실제 collector 신원·읽기 확인, 권한 경고 |
| `/v1/workloads` | `name`, `repo_url`, `app_path`, `iac_path`, `source_environment_id`, `namespace` | `workload_id`, 코드·실제 이미지 대조 결과, 수집 범위 |
| `/v1/plans` | `workload_id`, `target_environment_id` | `plan_id`, 출발지·목적지, 계획·수집 범위 digest, 초기 상태 `COLLECT`와 버전 1 |

세 경로 모두 검증된 운영자 Bearer와 단일 `Idempotency-Key`가 필요하다. `connection_ref`는 운영자가 설치한 tenant별 별칭이다. 요청 본문으로 API 주소, CA, 자격, 클러스터 UID, registry digest 또는 tenant를 지정할 수 없다. 같은 tenant·종류·키·운영자·입력이면 최초 영수증을 돌려준다. 최초 생성은 201, 재조회는 200이다. 재조회 시각이 최초 접속 점검 시각을 갱신하지 않는다.

## 등록 때 확인하는 것

1. **환경.** 고정된 HTTPS 주소·CA·TLS 이름을 사용하고 리다이렉트를 따르지 않는다. Kubernetes `SelfSubjectReview`로 namespace의 `bravo-collector` 신원을 확인한다. 같은 자격으로 지명된 CNPG Cluster의 get, Pod·Service list 권한을 각각 확인하고 실제 CNPG 객체의 namespace·이름·UID를 대조한다. Secret 값을 읽거나 DB에 로그인하지 않는다.
2. **권한 범위.** Pod 생성과 등록한 CNPG의 patch 권한을 같은 주체의 개별 접근 점검으로 확인해 `write_checks`에 허용·거부·불명을 담는다. `SelfSubjectRulesReview`에서 namespace 쓰기 권한이 보이면 `namespace_write_granted`를, 완전한 열거가 불가능하면 `write_scope_unknown`을 기록한다. 필수 읽기 점검의 거부·불명은 등록을 막는다. 경고의 범위는 해당 Kubernetes namespace다. AWS 계정 전체 IAM 권한의 부재를 인증하는 결과로 사용할 수 없다.
3. **앱.** 서버에 읽기 전용으로 준비한 checkout의 origin과 고정 commit, 앱 디렉터리·IaC 경로가 commit에 있는지 확인한다. 릴리스의 이미지 출처는 이미지 담당자가 게시·전달 증거를 확인해 설정한다. 출발지에서 지정 label의 모든 활성 Pod에 대해 배포 담당이 확인한 ReplicaSet 소유 UID·이름·서비스 계정·컨테이너·요청 이미지·실행 imageID·Ready를 대조한다. 빈 결과, 페이지 절단, 이미지가 섞인 롤아웃, replica별 참조 차이는 등록을 막는다. 실제 컨테이너가 참조한 Secret·ConfigMap 이름, 서비스 계정, CNPG를 수집 범위에 넣는다. 환경 변수의 값과 Secret 본문은 저장하지 않는다.
4. **계획.** 앱의 현재 배포와 양쪽 환경을 다시 읽는다. 앱 등록 후 수집 범위가 바뀌면 새 시점의 앱 등록이 필요하다. 현재 앱·환경·수집 범위에서 digest를 만들고, 기존 계획 잠금 안에서 `PlanSpec`, 첫 상태 이벤트, 등록 영수증을 함께 저장한다. T16 수집·의존성 등급·코드 전용/배포 전용 분류와 T19 통제 비교는 이 시작 상태의 후속 소비자다. 실측 창은 별도의 [생산자 권한과 저장 경로](T10-window-storage.md)를 사용한다.

등록 자료는 불변이다. 운영자가 환경을 교체할 때에는 같은 이름의 CNPG를 새 신원으로 오인하지 않도록 새 환경 ID와 연결 참조를 설치한다. 한 tenant의 등록을 다른 tenant의 앱이나 계획에서 사용할 수 없다. 외부 접속 검사는 DB 잠금 밖에서 수행한다. 관측 후 클러스터가 변할 수 있으므로 실제 수집과 실행은 각 단계에서 다시 확인해야 한다.

## 운영자 호스트 연결

기존 운영자 FastAPI 앱은 `install_registration_routes(app, registry, verify_identity=...)`로 세 경로를 설치할 수 있다. 호스트는 요청 검증 오류에 사용자가 보낸 자격·본문을 다시 싣지 않아야 한다. 등록 전용 ASGI entrypoint는 `python -m bravo_platform_events.registration_server`다. 운영자 TLS ingress와 기존 T10 runtime DB mount를 사용한다. T18 창 수신 Service·태그에 등록 경로를 노출하지 않는다.

`/registration/catalog.json`은 운영자 전용 읽기 전용 설정이다.

| 키 | 운영자가 준비할 값 |
|---|---|
| `operator_verifier` | 기존 [운영자 신원 검증기](T10-operator-access-v1-proposal.md)의 issuer, audience, client_id, assurance, 실제 사람의 subject→tenant 매핑, 공개 JWK |
| `environments[]` | `tenant_id, environment_id, connection_ref, kind, namespace, database, database_uid, endpoint, ca_file, token_file`. 검증된 포워딩 경로에만 `tls_name`을 지정한다. 전용 collector 자격은 회전 가능한 비공개 mount로 제공한다. |
| `releases[]` | `tenant_id, environment_id, repo_url, checkout, app_path, iac_path, revision, label, container, image, runtime_digests, service_account, replicaset_uid`. `label`은 Pod의 `app` 값, `image`는 registry가 포함된 digest 참조, `runtime_digests`는 같은 빌드의 허용 digest다. |
| 별도 `/registration/grants.json` | `[{"tenant_id":"tenant-example","subject_id":"operator-example"}]` 형식. 실제 subject ID는 신원 검증기의 `operator_id(issuer, sub)`로 구한다. 서버는 등록 직전과 저장 잠금 획득 뒤 파일의 현재 권한과 토큰 만료를 확인한다. |

서비스 프로세스에 관리자 kubeconfig, 개인 로그인, Executor 제어 자격을 주지 않는다. 등록된 상태는 [기존 계획 조회와 SSE](T10-plan-snapshot-v1-proposal.md)에서 같은 plan ID로 읽는다. 운영 배포 전 T6·T30 담당이 endpoint·CA·CNPG UID·SA와 실제 대상 환경을 확인하고, T21이 운영자 신원 설정을 인계해야 한다.

## 저장소 변경과 복구

SQLite는 기존 `PlanStore` 초기화로 준비한다. PostgreSQL은 migration 로그인으로 `services/platform-events/sql/0003_registrations.sql`을 적용한다. v2가 선행이고, 새 테이블에 runtime은 SELECT·INSERT만 가진다. 입력·시간·키·receipt는 append-only다.

운영 DB에서는 먼저 기존 v2 전용 프로세스를 v2/v3 호환 reader로 교체하고 재시작을 확인한다. DB 담당과 변경 시간을 정한 뒤 변경 전 백업·격리 복원을 확인하고 migration한다. 변경 뒤에는 기존 계획·창과 새 registrations의 행·유일성·권한까지 복원본과 대조한다. 등록 서버 기동은 v3에서만 허용한다. 아직 v2 전용 이미지가 있는 플랫폼 DB에 migration을 먼저 적용하면 해당 프로세스가 재시작할 때 실패한다.

| 실패 | 동작 |
|---|---|
| 403 | 운영자 신원, 등록 권한 또는 현재 토큰 만료를 확인한다. |
| 404 | 해당 tenant에서 참조를 사용할 수 없다. 다른 tenant의 존재는 노출하지 않는다. |
| 409 | 키가 충돌했거나 등록 후 환경·배포·수집 범위가 달라졌다. 담당자가 최신 원본을 대조한다. |
| 422 | 필드, 역할 형식 또는 멱등 키를 고친다. |
| 503 | 자격, TLS, 필수 읽기 권한, 불완전한 응답 또는 DB 접근을 복구한다. commit 응답이 유실됐으면 동일한 입력과 키로 결과를 조회한다. |
