# T6 runtime evidence · 2026-11-03

Foundation and workload observations below were collected by separate owners. Each section retains its original scope and timestamps; foundation readiness alone does not establish application readiness.

## EKS foundation — 2026-11-03

Actual AWS validation used the existing individual IAM operator in the reviewed T1 account. It did not use SSO.

| Check | Result |
|---|---|
| Terraform fmt/init/validate | PASS |
| Mock boundary tests | 3 PASS |
| Trivy HIGH/CRITICAL | 0 findings in this stack |
| EKS/nodegroup | ACTIVE; Kubernetes 1.35 |
| Kubernetes API | private endpoint; CA/TLS server name checked through SSM tunnel; `/readyz=ok` |
| Nodes | 1/1 Ready |
| Namespace ConfigMap read | actual request allowed |
| Secret read and other-namespace request | actual requests returned Forbidden |
| EKS audit canary | ConfigMap create event (201) observed in CloudWatch |
| Disposable test namespace | removed |
| Individual SSO | NOT VERIFIED |
| App/DB/CNPG/PgBouncer/ESO/cutover | separate workload follow-up |

[Machine-readable results](results.json) include the measured scope and hashes of the runtime source files. Raw provider responses, expected account, object IDs, command IDs, endpoint/CA and original canary namespace are preserved in the operator's private evidence directory. They are not published here. The workload verification session reuses this cluster and owns dependent workload resources separately. T6 remains in progress.

## 출발지 워크로드 실제 구축 검증 · 2026-11-03

사용자가 실제 개발 환경 구축을 요청했고, 기존 기반 배포 담당의 완료·소유권 인계 뒤 별도 `source-workloads` state와 새 `source-dev` namespace에서 작업했다. EKS 기반 state·VPC·NAT·기존 IAM OIDC provider·노드 역할·vpc-cni는 변경하지 않았다. 처음 Kubernetes 조회에서 사용자 namespace와 운영 데이터가 없음을 확인했다.

이 기록은 현재 팀 환경의 실제 AWS/Kubernetes/SQL 관측이다. 개인 B-free나 예선 배포 결과를 재사용하지 않았다. 실행 주체는 기존 개인 IAM operator이며 SSO/다른 팀원의 실제 접속 완료를 주장하지 않는다. 리소스 식별자·원문 로그·자격증명은 내보내지 않고 필요한 상태와 값만 저장했다.

| 범위 | 관측 |
|---|---|
| 관리 경로 | EKS private-only endpoint. 플랫폼 EC2의 SSM remote-host tunnel을 경유하고 로컬 AWS exec 인증으로 `/readyz=ok`, node Ready 1개 확인 |
| EKS | Kubernetes 1.35, nodegroup ACTIVE, control-plane 5종 로그 enabled. 이 상태는 별도 기반 담당의 자원에 대한 읽기 검증 |
| 보조 AWS | 최초 plan 24개 create, 기존 기반 변경/삭제 0. EBS CSI ACTIVE, IRSA 3역할, Secrets Manager 항목 2개, 첨부/감사 버킷, KMS, 전용 CloudTrail |
| CNPG | operator 1.30.1, PostgreSQL 18.4, Ready 1, encrypted gp3 PVC Bound; 단일 인스턴스 개발 구성 |
| PgBouncer | 1.25.1, Ready 1. 실제 `chat_app` 접속, client CA/DNS `verify-full`, DB측 TLSv1.3 조회 성공 |
| DB 권한 | 앱 superuser=false/schema CREATE=false, 실제 앱 DDL 거부. 별도 복제 역할, pooler 제어 역할은 PostgreSQL에 없음 |
| pooler 권한 | 앱의 관리 DB 접근 거부, 별도 제어 역할 관리 접근 허용, 틀린 암호 및 평문 클라이언트 접속 거부 |
| 논리 복제 준비 | `wal_level=logical`, publication `source_chat` = attachments/messages, 앱 테이블 PK 누락 0. 환경 간 subscription/왕복 복제는 미검증 |
| ESO | 별도 service-account IRSA, namespace SecretStore Ready, ExternalSecret SecretSynced. 앱 DB 암호의 직접 Secrets Manager SDK 의존은 유지 |
| DB 감사 | `log_connections=on`, pgAudit `ddl,role`, parameter logging off; 실제 접속 및 구조화된 DDL canary 관측 |
| K8s 감사 | 전용 RoleBinding 카나리가 EKS CloudWatch audit 로그에 실제 관측됨 |
| AWS 감사 | CloudTrail enabled, 전달 오류 없음. S3에 전달된 gzip JSON을 메모리에서 읽어 이번 `PutSecretValue` 2건(08:37:48Z) 확인. KMS 암호화·로그 검증·관리/S3 data selector 설정 |

`database-audit-readback.json`의 관측 시각은 **2026-11-03T08:41:05.531768Z**, `cloudtrail-delivery.json`은 **08:42:24.463076Z**다. DB canary는 이름이 고정된 전용 스키마/시험 테이블이며 업무 테이블 publication에 포함하지 않았다. raw SQL/AWS error를 출력하지 않았다.

초기 EBS PVC는 암호화되어 있었지만 component 태그가 없어 실제 조회로 발견했다. EBS add-on의 공식 configuration schema에 있는 `controller.extraVolumeTags`를 선언해 이후 볼륨에도 비용 태그를 적용하고, 이미 생성한 **자기 namespace의 PVC 볼륨 1개**에만 source/project 태그를 보완·재조회했다. 기반 노드 볼륨은 수정하지 않았다.

로컬 검증은 최초 repository unittest 91개(신규 credential/계약/감사 회귀 8개 포함), 최신 develop 통합 후 110개, 독립 검토 수정 후 112개(신규 10개), 기존 T6 계약 검사 18개, Ruff(py39), Terraform fmt/validate, Trivy config HIGH/CRITICAL, Gitleaks 8.24.3과 공개 식별자 검사다. 설치 후 실제 server-side dry-run과 rollout/readback은 별도 수행했다. CI·팀 리뷰·병합 결과는 PR에서 따로 확인한다.

독립 검토에서 T8 역할의 HBA가 물리 복제까지 허용함을 확인해 `hostssl replication chat_replicator ...`를 제거했다. 이 특별한 `replication` DB 항목은 [PostgreSQL 18 문서](https://www.postgresql.org/docs/18/auth-pg-hba-conf.html)상 물리 복제용이다. 실제 source PostgreSQL 18.4에 변경을 적용하고 **09:01:27.268626Z**에 `replication=database` 연결의 `IDENTIFY_SYSTEM` 성공, `replication=true` 연결의 HBA 거부를 확인했다. 암호/시스템 ID는 내보내지 않았다. `replication-boundary-readback.json`에 보존한다. CNPG의 별도 내부 streaming replica 규칙은 변경하지 않았다.

이 replication JSON은 당시의 별도 bounded operator probe 결과다. `verify.py` 전체를 그 시각에 실행한 결과는 아니다. 당시에는 수정된 working tree를 렌더링해 적용했고 실행 전 source hash는 따로 저장하지 않았다. 이후 동일한 명령을 `verify.py`에 재현 경로로 추가했다. 사후 커밋/해시를 당시 실행 전 증거로 소급하지 않는다.

동일 검토에서 이미지 검사의 접미사/하위 저장소 허용도 수정했다. 정확한 팀 `demo-chat` 저장소, 선택적인 유효 Docker tag, SHA-256 digest만 받는다. 비슷한 저장소·하위 경로·다른 조직·빈/잘못된 tag 거부 회귀와 독립 시험 5개가 통과했다. 게시된 앱 digest를 넣은 최종 Deployment는 server-side dry-run을 통과했으며 실제 pull/rollout/API는 별도 완료 조건이다.

`workload-identity-readback.json`은 **08:53:15.961942Z** 실제 역할 경계 검사다. namespace의 SA TokenRequest를 `sts.amazonaws.com` audience로 발급하고 STS web identity 교환 결과는 메모리에만 두었다. 앱 역할은 DB secret 읽기 허용/control secret 거부, ESO 역할은 control 읽기 허용/DB secret 및 첨부 목록 거부를 실제 AWS API에서 확인했다. 앱 역할로 합성 문자열 1개를 첨부 버킷에 쓰고 읽어 byte 일치·KMS 암호화를 확인했다. 이는 IAM/S3 SDK 경로의 실측이며 아직 앱 HTTP 첨부 API의 성공을 뜻하지 않는다.

### T14 수집기 실제 연결

**08:57:03.516623Z**에 병합된 T14 라이브러리 `2ec5ec834c63f75ff8432c59c1224d1fa4ac2ef2`의 `SecretAccessProbe`와 `S3AnonymousReadProbe`를 위 환경에 연결했다. operator 프로세스에서 두 STS 세션의 실제 계정/assumed-role을 독립 조회해 등록 역할과 대조한 뒤 bounded SDK client(connect/read timeout 5초, standard total attempts 2)를 넘겼다. 단기 자격증명은 메모리에만 있었고 새 키를 만들지 않았다.

- `SEC-01.1`: 고정한 DB secret AWSCURRENT version의 앱 읽기 성공, 다른 역할(ESO) 읽기 AccessDenied → **PASS**.
- `IAM-01.1`: 고정 version의 시험 전용 객체를 앱 일반 GetObject로 읽고, 서명 없는 일반 GetObject 거부 뒤 앱 일반 GetObject 재성공 → **PASS**. GetObjectVersion으로 권한을 바꾸지 않았다.
- `full` 프로필 20행 중 **나머지 18행은 UNKNOWN/NOT_RUN**이다. 전체 게이트 통과나 T14 전체 완료가 아니다. 이 두 증거도 24시간의 만료 시각을 가진 관측이다.

[`t14-source/control-run.json`](t14-source/control-run.json)과 canonical collector bytes를 보존했다. 각 결과의 `output_sha256`를 실제 보존 파일과 대조했고, 복사 과정에서도 bytes를 바꾸지 않았다. ARN·계정·버킷/key·Secret 값 대신 수집기의 resource/principal hash만 포함한다. 이 실행은 trusted operator의 실제 연결 시험이며 서비스의 사용자 인증/등록 API까지 통합한 결과는 아니다.

시크릿 게시 첫 시도는 macOS AWS CLI stdin paramfile 처리에서 종료 252로 실패했다. 값은 출력하지 않았고, Boto3의 메모리 호출로 전환해 성공했다. 이미 AWS에 값이 있으면 동일함을 확인해 재사용하며 다르면 덮어쓰지 않는다. 기존 비밀번호를 갱신하거나 새 장기 키를 만들지 않았다.

위 초기 JSON 시점의 배포 목록은 PgBouncer만 포함하며 demo-chat 배포 성공을 그 JSON으로 주장하지 않는다. 후속 앱 검증은 아래에 별도 시각과 실행 소스로 기록한다. T8의 원격 DB 라우팅·왕복 복제, T12 실제 cutover, T14 통제 비교, T35 백업·복원, 고가용성은 각각 별도 후속 검증이다. source 자원은 요청에 따라 후속 작업을 위해 유지한다.

### Private ECR 앱 배포와 실제 업무 경로

사용자가 GHCR 조직 공개 정책을 유지하기로 결정해, 별도 이미지 담당이 main Actions OIDC로 기존 OCI index를 private ECR에 복사했다. [복사·검증 run 41610561593](https://github.com/Softbank-Hackathon-Finals/demo-chat/actions/runs/41610561593)은 원본/목적지 index·2 child·15 blob 바이트 일치, 취약점/secret 0건 및 ECR 실제 pull/runtime probe를 통과했다. 소스는 `ce5727eb3e3d2355077afdaadcb1a722aba9c7cd`, OCI index는 `sha256:001adc35a9fc767566552fb55c3c19a67b7c5efc7231fb02dc28cdeace99d0d4`다. 공개 정책·기반 노드 역할은 바꾸지 않았고 장기 registry secret을 만들지 않았다.

출발지 operator는 private contract의 exact URL·현재 STS 계정·앱 역할 계정·region을 대조하고, ECR에서 가져온 index 원문 bytes의 SHA-256과 linux/amd64 child를 독립 확인한 뒤 server dry-run/apply했다. 배포 소스는 `6d8a8dfbc59dd8815d9f162e33977d7bf4b76c2d`다. **09:31:43.376935Z** 재조회에서 앱 Ready 1, 재시작 0, UID 10001, x86_64, web identity 존재/정적 AWS key 없음, `verify-full`과 CA 존재를 확인했다. 컨테이너 런타임의 imageID는 child가 아니라 **원본 index digest**를 보고했다. 이 차이를 [`application-deployment-readback.json`](application-deployment-readback.json)에 그대로 보존한다.

**09:28:39.541815Z**에 실행한 [`application-readback.json`](application-readback.json)은 실제 앱 HTTP 관측 **31개 모두 true**다. 시작 시 Secrets Manager SDK 조회를 요구하는 앱이 기동했고, DB Secret → PgBouncer → PostgreSQL 업무 query와 S3 첨부 경로를 사용했다.

- 메시지 생성 201, 동일 멱등 키 재시도 200/같은 ID, 다른 내용 재사용 409, 저장한 메시지 재조회가 성공했다.
- 첨부 upload/download와 SHA-256 byte 대조, DB 첨부 참조 연결·재조회가 성공했다. 합성 메시지 2개와 첨부 1개를 후속 복제 시험을 위해 보존했다.
- 누락/잘못된 관리자 인증 401, 처음·마지막의 업무 쓰기 503과 Retry-After를 확인했다. 시험 후 `write_blocked=true`, 복원 1회 성공, 일시 실패 0, `manual_write_block_required=false`다. 차단 뒤에도 읽기/healthz/readyz/DB health는 200이었다. 09:31 재조회에서도 차단 상태였다.

앱 시험 실행 전 **`27b0ef46f89c26c6a11c11b4aaf0bfc94a52d75b`**와 probe hash를 [`application-probe-source.json`](application-probe-source.json)에 기록했다. 실행 뒤 독립 검토가 추가로 찾은 HTTP 본문 단절/잘못된 JSON shape의 복원 경계는 `1e2f33e`에서 보완하고 모의 실패 시험으로 검증했다. 그 후속 코드를 09:28 실행 소스로 소급하지 않는다. 잘못된 admin 시험은 차단 요청만 보내며, 복원은 PUT 실패 후 GET readback·최대 3회·영구 실패 시 수동 차단 필요 표시를 사용한다. 최신 전체 scripts **131개**, 독립 HBA/image/복원 경계 **9개**가 통과했다.

09:29 재조회는 최근 20분 창에 이전 감사 canary가 없어 DDL/K8s canary 관측이 false였다. 09:30에 전용 canary를 다시 실행한 직후 DB DDL은 관측됐지만 EKS 전달은 아직 false였고, **09:31:44.865853Z** 재조회에서 둘 다 true가 됐다. 중간 false 결과를 덮어쓰지 않고 `final-runtime-readback.json`, `fresh-audit-readback.json`, [`final-audit-delivery-readback.json`](final-audit-delivery-readback.json)을 모두 보존했다. 마지막 조회는 앱/pooler Ready 1, DB/PVC/ESO 정상, 논리 복제 허용·물리 복제 거부, CloudTrail 전달 정상도 확인한다.

[`application-evidence-manifest.json`](application-evidence-manifest.json)은 선택 필드만 수집한 비공개 원본과 공개 사본의 byte/hash 일치를 기록한다. 원문 Secret·자격증명·AWS 계정/ECR URL은 수집·공개하지 않았다. 이 결과는 source 개발 환경 완료 근거이며 환경 간 연결·전환·동등성 전체 완료 근거는 아니다.

배포·재검증과 소비 계약: [`infra/envs/source-workloads/README.md`](../../../infra/envs/source-workloads/README.md). 공개 기록은 실제 계정/ARN/endpoint나 자격증명 전달 경로를 포함하지 않는다.
