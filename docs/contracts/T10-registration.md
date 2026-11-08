# 환경, 앱, 이전 계획 등록

운영자는 읽기 주체로 환경을 확인하고, 실행 중인 앱을 검토한 코드와 이미지에 연결한 뒤 이전 계획을 등록한다. 환경 종류는 `aws`(EKS) · `gcp`(GKE) · `onprem`(RKE2)이고 모든 종류가 출발지와 목적지가 될 수 있다([ADR-0034](../adr/0034-environment-kind-adapters.md), 종류 원본은 [environment-kinds.json](environment-kinds.json)). 같은 종류 간 이전은 다른 클러스터로의 이전이다. [설계 §8.9·8.11](../design.html#s8-11) · [T10 계획 저장 계약](T10-plan-snapshot-v1-proposal.md)

| POST | 요청 본문 | 최초 응답 |
|---|---|---|
| `/v1/environments` | `name`, `kind`(`aws` · `gcp` · `onprem`), `roles`([`source`] 또는 [`target`]), `connection_ref` | 서버가 부여한 `environment_id`, 실제 collector 신원·읽기 확인, 권한 경고 |
| `/v1/workloads` | `name`, `repo_url`, `app_path`, `iac_path`, `source_environment_id`, `namespace` | `workload_id`, 코드·실제 이미지 대조 결과, 수집 범위 |
| `/v1/plans` | `workload_id`, `target_environment_id`, `profile_id`, `profile_version` | `plan_id`, 출발지·목적지, 평가 프로필, 계획·수집 범위 digest, 초기 상태 `COLLECT`와 버전 1 |

세 경로 모두 검증된 운영자 Bearer와 단일 `Idempotency-Key`가 필요하다. `connection_ref`는 운영자가 설치한 tenant별 별칭이다. 요청 본문으로 API 주소, CA, 자격, 클러스터 UID, registry digest 또는 tenant를 지정할 수 없다. 같은 tenant·종류·키·운영자·입력이면 최초 영수증을 돌려준다. 최초 생성은 201, 재조회는 200이다. 재조회 시각이 최초 접속 점검 시각을 갱신하지 않는다.

## 등록 때 확인하는 것

1. **환경.** 고정된 HTTPS 주소·CA·TLS 이름을 사용하고 리다이렉트를 따르지 않는다. Kubernetes `SelfSubjectReview`로 namespace의 `bravo-collector` 신원을 확인한다. 같은 자격으로 지명된 CNPG Cluster의 get, Pod·Service list 권한을 각각 확인하고 실제 CNPG 객체의 namespace·이름·UID를 대조한다. Secret 값을 읽거나 DB에 로그인하지 않는다.
2. **권한 범위.** Pod 생성과 등록한 CNPG의 patch 권한을 같은 주체의 개별 접근 점검으로 확인해 `write_checks`에 허용·거부·불명을 담는다. `SelfSubjectRulesReview`에서 namespace 쓰기 권한이 보이면 `namespace_write_granted`를, 완전한 열거가 불가능하면 `write_scope_unknown`을 기록한다. 필수 읽기 점검의 거부·불명은 등록을 막는다. 경고의 범위는 해당 Kubernetes namespace다. AWS 계정 전체 IAM 권한의 부재를 인증하는 결과로 사용할 수 없다.
3. **앱.** 서버에 읽기 전용으로 준비한 checkout의 origin과 고정 commit, 앱 디렉터리·IaC 경로가 commit에 있는지 확인한다. 릴리스의 이미지 출처는 이미지 담당자가 게시·전달 증거를 확인해 설정한다. 출발지에서 지정 label의 모든 활성 Pod에 대해 배포 담당이 확인한 ReplicaSet 소유 UID·이름·서비스 계정·컨테이너·요청 이미지·실행 imageID·Ready를 대조한다. 빈 결과, 페이지 절단, 이미지가 섞인 롤아웃, replica별 참조 차이는 등록을 막는다. 실제 컨테이너가 참조한 Secret·ConfigMap 이름, 서비스 계정, CNPG를 수집 범위에 넣는다. 환경 변수의 값과 Secret 본문은 저장하지 않는다.
4. **조합.** 계획의 목적지는 `target`으로 등록된 환경이어야 하고, 출발지와 DB 신원이 달라야 한다. 양쪽에 클러스터 신원이 있으면 서로 달라야 하고(`same_cluster_identity`), 같은 종류 간 계획은 양쪽 모두 클러스터 신원이 있어야 한다(`cluster_identity_unverified`). 예외로 클러스터 신원 도입 전부터 받던 onprem→onprem은 DB 신원만 달라도 계속 받는다. 이어서 조합 준비도를 계산한다. 출발지 종류에 `source.*` · `both.*` 칸이, 목적지 종류에 `target.*` · `both.*` 칸이 모두 있어야 하고, 빠진 첫 칸(계약 순서)을 `adapter_missing:<칸>`(409)으로 돌려준다. 이 검사들은 환경을 다시 읽기 전에 끝나므로 거부된 계획은 probe를 부르지 않는다.
5. **계획.** 앱의 현재 배포와 양쪽 환경을 다시 읽는다. 앱 등록 후 수집 범위가 바뀌면 새 시점의 앱 등록이 필요하다. 현재 앱·환경·수집 범위에서 digest를 만들고, 기존 계획 잠금 안에서 `PlanSpec`, 첫 상태 이벤트, 등록 영수증을 함께 저장한다. T16 수집·의존성 등급·코드 전용/배포 전용 분류와 T19 통제 비교는 이 시작 상태의 후속 소비자다. 실측 창은 별도의 [생산자 권한과 저장 경로](T10-window-storage.md)를 사용한다.

등록 자료는 불변이다. 운영자가 환경을 교체할 때에는 같은 이름의 CNPG를 새 신원으로 오인하지 않도록 새 환경 ID와 연결 참조를 설치한다. 한 tenant의 등록을 다른 tenant의 앱이나 계획에서 사용할 수 없다. 외부 접속 검사는 DB 잠금 밖에서 수행한다. 관측 후 클러스터가 변할 수 있으므로 실제 수집과 실행은 각 단계에서 다시 확인해야 한다.

### 계획의 평가 프로필과 앱 식별자

통제 원문과 게이트의 `app`은 계획의 생성된 `workload_id`다. 앱 표시 이름이나
카탈로그 프로필의 기준 앱 이름으로 바꾸지 않는다. 새 계획 요청에는 문자열
`profile_id`와 `profile_version`이 모두 필요하다. 두 값은 영문·숫자로 시작하고
영문·숫자·밑줄·점·하이픈을 포함한 128자 이하다. 호스트는 tenant별 허용 목록을
확인한 뒤에만 환경을 조회한다. 허용 목록이 없거나 값이 다르면
`evaluation_profile_unavailable`(409)로 거부한다. 누락·형식 오류는 422다.

두 필드는 `PlanSpec` 최상위, 계획 digest 입력, 등록 영수증과 계획 조회에 함께
저장된다. 같은 키의 다른 프로필은 `command_key_conflict`다. T10의
`GateRecords.bind`와 저장 컨텍스트 조회는 게이트 단계에 고정된 프로필과 정확히 일치해야 한다.
T19는 동일한 spec과 현재 카탈로그를 별도로 대조한다. 허용 목록은 등록 정책이며
카탈로그의 존재·무결성이나 통제 시험 결과를 대신하지 않는다.

### 반영 후 재대조 프로필

T31 반영 뒤 재대조할 새 계획은 `recheck_profile_id`·`recheck_profile_version`도
등록 요청에 함께 명시한다. 두 필드의 문자열 규칙은 최초 프로필과 같고,
한 필드만 있거나 명시한 값이 `null`이면 422다. 두 필드를 생략한 기존 요청은
기존 원문 그대로 재시도할 수 있지만 그 계획은 반영 후 게이트를 열 수 없다.
재대조 프로필도 같은 tenant의 `evaluation_profiles` 허용 목록에 있어야 하며,
환경 조회 전에 검사한다. 두 쌍 모두 spec·plan digest·등록 영수증·snapshot에 고정한다.

최초 `demo-gate:1`, 재대조 `apply-recheck:1`을 쓰는 운영자는 허용 목록에 각각
`{"tenant_id":"tenant-example","profile_id":"demo-gate","profile_version":"1"}`와
`{"tenant_id":"tenant-example","profile_id":"apply-recheck","profile_version":"1"}`을
등록한다. 카탈로그와 T19·T21 소비자가 지원하는 버전인지 먼저 확인해야 한다.
`full:1`이나 최초 프로필로 자동 대체하지 않는다. 기존 계획을 수정하는 대신
현재 앱·환경을 다시 확인해 새 키로 새 계획을 등록한다.

`GateRecords.bind(..., gate_phase="post_apply", application_id=...)`는 성공한
`WorkflowRecovery` Application 체크포인트와 같은 application ID일 때만
재대조 쌍을 선택한다. 반영 접수 claim만으로는 충분하지 않다. 새 결속은 `GATE`
상태에서 만들고, 같은 결속 재시도는 기존 원문을 반환하며 과거 revision을
재활성화하지 않는다. 반영 기록이 생기면 최초 컨텍스트는 소비할 수 없다.
초기 결속은 기존 저장 형식을 유지한다. `GateContext`의 원문과 fingerprint는 바꾸지 않는다.

T10 view는 `PlanGateSession`에 `gate_phase`(`initial` 또는 `post_apply`)와
`application_id`(최초는 `None`)를 전달한다. T19는 단계에 맞는 spec 필드와
현재 권한·원문을 별도로 검증하고, T21은 snapshot의 선택 필드와 해당 단계의
보고서 프로필을 소비한다. 반영 후 세션은 단계와 application ID를 fingerprint에 결속하고, 최초 세션은 기존 fingerprint를 유지한다.

운영 호스트는 `records.cutover_authority(upstream=..., load_gate=...)`를 기존
CutoverService의 authority로 전달한다. `upstream`은 현재 운영 권한·SHIFT
조건을 검증한 CurrentCutover를 반환하고, `load_gate(view)`는 현재 인증된
T19 service와 readiness를 반환한다. 조립 함수가 같은 트랜잭션의
`view.cutover_binding(binding)`으로 `CurrentCutover.binding`을 만든다.
이 함수는 계획·gate ID·context digest·목적지 일치 여부를 확인하고 저장된
`gate_phase`·`application_id`를 넣는다. 이미 다른 단계나 application ID가
들어 있는 binding은 고쳐 쓰지 않고 거부한다. T19 `_bind`는 세션과 이 binding의
두 필드를 대조한다. 두 필드가 모두 없는 과거 binding은 최초 단계에서만 허용하며, 부분 누락이나 post_apply의 누락은 거부한다. 승인 요청·저장 이벤트·멱등 재요청에도 두 값이 남으므로
최초 승인 본문을 반영 후 세션의 승인으로 재사용할 수 없다. 새 메타데이터가
없는 과거 승인 이벤트의 원문은 그대로 읽을 수 있다.

T10 `GateRecheck.evaluate`는 준비 콜백 호출 전에
`records.recheck_profile(principal, plan_id, application_id)`로 저장된 쌍과
Application 체크포인트를 확인한다. 기존 `prepare(principal, plan_id, outcome)`
콜백도 이 쌍을 읽어 새 source·target 원문을 인증하고 위 `post_apply` 결속으로 저장한다.
목적지 run과 SEC-01.1·IAM-01.1·ENC-01.1 증거는 반영 완료 시각 이후여야 한다.
Pod·이미지·자격 공급원이 바뀐 앱은 T14 등록 경계를 새로 확인해야 하며,
그 증거가 없으면 수집기/T19의 UNKNOWN을 PASS로 바꾸지 않는다.

프로필이 없는 기존 계획과 원문은 보존한다. 읽기는 가능하지만 새 게이트 결속은
거부한다. 기존 계획 spec·digest를 수정하거나 기본 프로필을 추정하지 않는다.
기존 환경·앱 등록이 여전히 유효하면, 같은 등록 ID와 명시한 프로필 및 **새 키**로
새 계획을 만든다. 이 과정은 출발지 앱과 양쪽 환경을 다시 확인하며 새 plan ID와
digest를 발급한다. 기존 키에 프로필을 추가해 요청하면 충돌한다. 변경 전 본문의
HTTP 재요청도 새 필수 필드가 없어 422이므로, 과거 결과는 기존 계획 조회에서 읽는다.

기존 등록 담당이 새 영수증을 인수한 뒤 새 계획을 참조하는 TaskCatalog·권한·수집
증거·게이트 컨텍스트를 발급한다. 이전 plan ID·digest에 결속된 자료를 새 계획의
증거로 재표기하지 않는다. 환경·앱을 중복 등록하거나 기존 감사 자료를 삭제할 필요는 없다.
[식별자 결정과 담당 분담](https://github.com/Softbank-Hackathon-Finals/platform/issues/29#issuecomment-6661068881)

## 운영자 호스트 연결

기존 운영자 FastAPI 앱은 `install_registration_routes(app, registry, verify_identity=...)`로 세 경로를 설치할 수 있다. 호스트는 요청 검증 오류에 사용자가 보낸 자격·본문을 다시 싣지 않아야 한다. 등록 전용 ASGI entrypoint는 `python -m bravo_platform_events.registration_server`다. 운영자 TLS ingress와 기존 T10 runtime DB mount를 사용한다. T18 창 수신 Service·태그에 등록 경로를 노출하지 않는다.

`/registration/catalog.json`은 운영자 전용 읽기 전용 설정이다.

환경의 역할은 운영자가 `catalog_role`(`source` 또는 `target`)로 지정한다. 종류에서 역할을 추론하지 않으며 aws · gcp · onprem 모두 두 역할을 가질 수 있다. 예를 들어 온프렘 Kubernetes를 출발지로 등록하려면 운영자가 새 `environment_id`와 `connection_ref`를 만들고 해당 환경에 `"kind": "onprem", "catalog_role": "source"`를 지정한다. 한 별칭에는 역할 하나만 둔다(이중 역할 불가). 같은 클러스터를 두 역할로 쓰려면 새 `environment_id`와 `connection_ref`를 따로 만든다. 등록 요청의 `roles`에는 서버에 지정한 역할 하나를 보낸다. 출발지 앱은 기존 CNPG·ReplicaSet·이미지 검사를 통과해야 한다.

**호환(deprecated):** P0 설정에서 `catalog_role`을 생략하면 `aws`는 출발지, `onprem`은 목적지로 동작하고 기동 시 DeprecationWarning을 남긴다. `gcp`와 이후 종류는 `catalog_role`이 없으면 기동하지 않는다(`environment_role_required`). 새 설정은 모든 환경에 `catalog_role`을 쓴다.

**클러스터 신원:** `cluster_uid`에 해당 클러스터 `kube-system` namespace의 `metadata.uid`를 넣으면 probe가 같은 collector 자격으로 `GET /api/v1/namespaces/kube-system`을 읽어 대조하고, 영수증 `checks.cluster_identity`에 SHA-256 값만 남긴다. collector에는 `resourceNames: [kube-system]`으로 좁힌 namespaces `get` ClusterRole이 추가로 필요하다. 값을 지정하지 않은 기존 환경은 그대로 동작하지만 같은 종류 간 계획에는 쓸 수 없다. 이미 등록한 환경의 `cluster_uid`가 바뀌면 `environment_changed`다.

**조합 준비도:** 기본값은 패키지 데이터 `bravo_platform_events/adapter_readiness.json`이고, ADR-0034 A1~A11에서 구현된 칸을 적는다. 세 종류 모두 `source.*`(A3 정적 규칙 · 배포 추적, A5 중립 내보내기, A8 출구 범위) · `target.catalog`(A4) · `target.neutral_apply`(A5 · A6) · `target.build`(A10) · `both.*`(A7 통제, 등록 신원)를 갖고, `target.rehearsal`은 onprem만 갖는다(A5, aws · gcp는 UNSUPPORTED). 그래서 기본 설정에서 계획 등록이 통과하는 조합은 목적지가 onprem인 aws→onprem · gcp→onprem · onprem→onprem이고, aws · gcp 목적지는 `adapter_missing:target.rehearsal`로 거부된다. 칸이 바뀌면 이 파일을 갱신한다. 운영자는 `catalog.json`의 `adapter_readiness`(`{"<종류>": ["<칸>", ...]}`)로 종류별 항목을 통째로 바꿀 수 있다. 계약에 없는 종류·칸은 기동을 막는다.

| 키 | 운영자가 준비할 값 |
|---|---|
| `operator_verifier` | 기존 [운영자 신원 검증기](T10-operator-access-v1-proposal.md)의 issuer, audience, client_id, assurance, 실제 사람의 subject→tenant 매핑, 공개 JWK |
| `environments[]` | `tenant_id, environment_id, connection_ref, kind, catalog_role, namespace, database, database_uid, endpoint, ca_file, token_file`, 선택 `cluster_uid`. 검증된 포워딩 경로에만 `tls_name`을 지정한다. 전용 collector 자격은 회전 가능한 비공개 mount로 제공한다. |
| `releases[]` | `tenant_id, environment_id, repo_url, checkout, app_path, iac_path, revision, label, container, image, runtime_digests, service_account, replicaset_uid`. `label`은 Pod의 `app` 값, `image`는 registry가 포함된 digest 참조, `runtime_digests`는 같은 빌드의 허용 digest다. |
| `adapter_readiness` | 선택. 종류별 준비된 어댑터 칸 목록으로 패키지 기본값의 해당 종류 항목을 바꾼다. 실제로 구현·검증된 칸만 적는다. |
| `evaluation_profiles[]` | 운영자가 현재 T5 카탈로그와 정책을 확인한 허용 목록. 예: `[{"tenant_id":"tenant-example","profile_id":"full","profile_version":"1"}]`. 누락 시 새 계획 등록을 거부한다. 앱 이름으로 프로필을 선택하거나 기본값을 넣지 않는다. |
| 별도 `/registration/grants.json` | `[{"tenant_id":"tenant-example","subject_id":"operator-example"}]` 형식. 실제 subject ID는 신원 검증기의 `operator_id(issuer, sub)`로 구한다. 서버는 등록 직전과 저장 잠금 획득 뒤 파일의 현재 권한과 토큰 만료를 확인한다. |

서비스 프로세스에 관리자 kubeconfig, 개인 로그인, Executor 제어 자격을 주지 않는다. 등록된 상태는 [기존 계획 조회와 SSE](T10-plan-snapshot-v1-proposal.md)에서 같은 plan ID로 읽는다. 운영 배포 전 T6·T30 담당이 endpoint·CA·CNPG UID·SA와 실제 대상 환경을 확인하고, T21이 운영자 신원 설정을 인계해야 한다.

### 기존 AWS SSO를 쓰는 등록 전용 선택지

등록 호스트만 `identity_mode: "aws_sso"`를 선택할 수 있다. `operator_verifier` 대신
`sso_verifier`에 `region`, `audience`, `expected_account`, `expected_caller_arn`,
`expected_sso_role_arn`, `expected_sso_role_id`, `tenant_id`를 설치한다. 두 검증기를
동시에 설정하면 시작을 거부한다. 기본 JWT 방식과 T21·S2·S3 검증기는 바뀌지 않는다.

운영자는 현재 SSO의 STS Account·Arn·UserId와 IAM GetRole의 Arn·RoleId를 확인한 뒤
정확한 사용자 세션을 설정한다. 역할 전체나 요청의 tenant 값으로 자동 등록하지 않는다.
별도 `grants.json`의 subject는 `registration_subject(account, role_id, caller_arn)`로
구한다. 등록 전과 DB 잠금 뒤 현재 grant와 증명 만료를 다시 확인한다.

클라이언트는 선택 의존성 `sso-registration`과
`make_registration_token(profile_name=..., region=..., audience=...)`를 사용한다.
명시한 프로필의 실제 credential provider가 `sso`일 때만 임시 자격으로 서명한다.
환경 키·역할 체인·인스턴스 자격으로 대체하지 않는다. 반환값은 TLS 요청의 Bearer에만
넣고 출력하거나 저장하지 않는다. 서버·Pod에는 개인 SSO 캐시나 AWS 자격을 설치하지 않는다.
`boto3`·`botocore`·`urllib3`와 개별 하위 로거의 DEBUG, HTTP wire 진단이 켜져 있으면
자격을 읽거나 갱신하기 전에 증명 생성을 거부한다. 부모 로거만 조용하게 설정해서는
하위 로거의 명시적 DEBUG 설정이 꺼지지 않는다. 호스트는 요청 중 로그 설정을 변경하지 않는다.

증명은 고정 regional STS `GetCallerIdentity`의 SigV4 서명 URL이다. 서명된
`x-bravo-registration-id`에 호스트의 audience를 결속한다. 검증기는 호스트·메서드·
Action·서명 범위·단일 쿼리 키를 제한하며, 리다이렉트·환경 프록시를 사용하지 않는다.
서버가 서명 시각부터 **60초**를 직접 강제한다. STS의 `X-Amz-Expires` 처리에 의존하지
않는다. 현재 STS 응답의 Account·Arn·UserId 전체가 설정된 사용자와 역할 ID에 맞아야 한다.
검증 실패는 403, AWS 조회 불명은 세부 정보 없는 503이다.

이 증명은 현재 AWS 임시 자격의 소유를 확인한다. 최초 SSO 로그인 시각이나 MFA 수행을
증명하지 않는다. 반환 주체는 등록 전용 `SsoRegistrationPrincipal`이며 승인용
`OperatorPrincipal`로 변환하지 않는다. 60초 동안의 같은 Bearer 재사용은 가능하지만,
등록 쓰기는 기존 Idempotency-Key·현재 권한 검사로 제한한다. 운영자는 해당 SSO 역할의
신뢰 정책이 다른 주체의 동일 세션 이름 가장을 허용하지 않는지도 확인해야 한다.

근거: [AWS GetCallerIdentity](https://docs.aws.amazon.com/STS/latest/APIReference/API_GetCallerIdentity.html),
[AWS IAM Authenticator의 서명된 서비스 식별자와 STS 검증](https://github.com/kubernetes-sigs/aws-iam-authenticator/blob/master/pkg/token/token.go).

## 저장소 변경과 복구

SQLite는 기존 `PlanStore` 초기화로 준비한다. PostgreSQL은 migration 로그인으로 `services/platform-events/sql/0003_registrations.sql`을 적용한다. v2가 선행이고, 새 테이블에 runtime은 SELECT·INSERT만 가진다. 입력·시간·키·receipt는 append-only다.

운영 DB에서는 먼저 기존 v2 전용 프로세스를 v2/v3 호환 reader로 교체하고 재시작을 확인한다. DB 담당과 변경 시간을 정한 뒤 변경 전 백업·격리 복원을 확인하고 migration한다. 변경 뒤에는 기존 계획·창과 새 registrations의 행·유일성·권한까지 복원본과 대조한다. 등록 서버 기동은 v3에서만 허용한다. 아직 v2 전용 이미지가 있는 플랫폼 DB에 migration을 먼저 적용하면 해당 프로세스가 재시작할 때 실패한다.

| 실패 | 동작 |
|---|---|
| 403 | 운영자 신원, 등록 권한 또는 현재 토큰 만료를 확인한다. |
| 404 | 해당 tenant에서 참조를 사용할 수 없다. 다른 tenant의 존재는 노출하지 않는다. |
| 409 | 키가 충돌했거나 등록 후 환경·배포·수집 범위가 달라졌다. 담당자가 최신 원본을 대조한다. `adapter_missing:<칸>`은 그 조합의 어댑터가 아직 없다는 뜻이고, `same_cluster_identity` · `cluster_identity_unverified`는 목적지 클러스터를 다시 고른다. |
| 422 | 필드, 역할 형식 또는 멱등 키를 고친다. |
| 503 | 자격, TLS, 필수 읽기 권한, 불완전한 응답 또는 DB 접근을 복구한다. commit 응답이 유실됐으면 동일한 입력과 키로 결과를 조회한다. |
