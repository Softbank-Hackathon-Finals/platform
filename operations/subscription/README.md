# 구독 워커 무인 실행

관리자가 한 번 연결한 Bravo 구독을 지정 VM에서 재사용한다. 실행 요청은 GitHub
Actions OIDC로 인증하고, VM이 자체적으로 access/refresh token을 갱신한다.
모델 프로세스에는 inference access token만 전달한다. AWS 자격과 refresh token은
broker 메모리에만 존재하며 인증 값은 GitHub Secrets나 파일로 복사하지 않는다.

## 권한과 실행

`infra/envs/subscription-runtime`은 기존 T7 노드 역할을 바꾸지 않고 전용 역할 두 개를
만든다. controller는 지정 인스턴스의 고정 SSM 문서를 호출하고 고정 포트 터널을 열 수
있다. broker는 팀 구독 Secret 한 개의 Get/Put/UpdateVersionStage만 허용한다.
두 역할 모두 immutable repository ID, STS audience, 정확한 ref에 결속한다.
검증 브랜치의 trust는 고정 만료시각을 갖고, 운영 경로는 보호된 develop이다.
broker 역할의 AWS trust와 전용 STS endpoint 보안 그룹은 broker용 보조 사설 IPv4만
허용한다. T7 운영자는 비공개 `platform-aws` 변수 `subscription_broker_ipv4`에 같은
서브넷의 미사용 주소를 지정한다. T7이 기본 ENI에 보조 주소를 할당하고 인스턴스 태그에
용도를 기록하면, T38은 그 주소가 실제로 할당된 보조 주소인지 확인한다.
일반 호스트와 Canal Pod가 사용하는 노드 기본 주소는 AWS에서 거부된다.

이 구성은 DHCP로 기본 주소 하나를 받는 T7 Ubuntu 노드에서 사용한다. 보조 주소는
EC2 ENI에만 할당한다. guest 인터페이스, netplan 주소 목록, 보조 주소를 자동 등록하는
도구에 넣으면 일반 프로세스도 그 주소에 bind할 수 있어 이 배치 조건을 위반한다.
설치기는 guest에 이미 등록된 주소를 거부한다. AMI 또는 네트워크 관리자를 바꾸면
새 호스트에서 이 조건부터 확인한다.

broker guard는 endpoint TCP 443에 한해 전달 패킷과 일반 호스트 연결을 차단한다.
broker slice의 연결만 허용하고 같은 nft 트랜잭션에서 출발 주소를 보조 IPv4로 바꾼다.
커널 conntrack이 응답을 원래 소켓에 전달한다. 부팅 때 guard가 없거나 처음 설치에
실패하면 주소 변환도 생기지 않으므로 AWS가 기본 주소에서 온 연결을 막는다.
broker는 guard 시작 성공을 요구한다. RKE2는 독립적으로 시작한다.

운영 순서는 T7의 보조 주소 할당, T38의 endpoint 보안 그룹·guard·역할 trust 적용,
broker slice 연결 성공과 일반 Pod·호스트 연결 거부 대조 순이다. 시험 Pod에서는 별도의
허용 목적지도 연결해야 한다. Terraform 재계획과 `nft list table inet bravo_broker_sts`로
규칙을 다시 읽는다. 사설 DNS를 끈 단일 AZ endpoint이므로 다른 STS 사용자는 기존 DNS를 쓴다.
스택 철거 시 먼저 실행을 닫고 broker trust와 endpoint를 제거한 뒤 호스트의 전용
guard 서비스·스크립트·slice·`sts.conf` 및 `bravo_broker_sts` 테이블을 정리한다.
마지막에 T7의 보조 주소를 해제한다.

controller는 GitHub 러너에서 같은 토큰으로 broker 역할의 직접 발급이 거부되는지 한 번 확인한다.
그 검사에서 발급을 잘못 허용해도 시험 세션에는 모든 API를 거부하는 정책이 적용된다.

`SessionDocumentAccessCheck`를 강제하여 DocumentName을 생략한 기본 shell도 거부한다.
워크플로는 임의 shell 명령, 기본 shell session, 임의 port session, Secret 목록 조회의
거부를 실제 호출로 확인한다. 허용된 SSM 문서는 start/stop/verify enum과 검증용
`runId`/`proposalDigest`만 받는다. 두 식별자는 고정 정규식으로 제한하며 verify에서는
빈 값을 거부한다. 사용자 명령·포트·manifest 입력은 받지 않는다. `GetCommandInvocation`은 AWS에서 리소스 수준
권한을 지원하지 않아 Resource `*`를 사용한다.
[AWS 권한 명세](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ssm.html).

터널은 AWS Session Manager의 TLS 채널을 사용한다. 워커의 HTTP listener는 VM loopback에만
바인딩하며 전달된 OIDC token을 STS에서 검증한다. broker의 지속시간은 최대 650초,
요청 수는 2회다. GitHub concurrency와 VM credential lock이 중복 갱신을 제한한다.
부팅 시 자동 시작이나 주기 실행은 설정하지 않는다.

## 팀 운영자 설치

기존 [관리자 최초 연결](../../services/agent-worker/subscription.md)에서 발급된 host ID와
팀 Secret을 재사용한다. host ID를 실행마다 새로 만들면 안 된다. `.collab/` 또는 로컬
비공개 설정을 커밋하지 않는다.

1. 팀의 암호화된 S3 상태 backend에 별도 `subscription-runtime` key로 Terraform을
   초기화한다. 변수는 정확한 OIDC provider/subject, VM ARN, 팀 Secret ARN, 검증 trust
   만료시각이다. Terraform은 Secret 값 자체를 읽거나 저장하지 않는다.
2. 운영자 전용 설정 JSON에는 `secret_arn`, `runtime_host_id`, `broker_role_arn`, `model`,
   `region` 메타데이터만 둔다. 토큰을 넣으면 installer의 필드 검사에서 거부한다.
3. 검토한 commit SHA의 codeload archive와 SHA256을 확인하고 다음 설치 도구를 실행한다.
   도구는 해당 archive와 uv 바이너리 해시를 검사하고, root 소유의 읽기 전용 코드와
   제한된 systemd unit을 배치한다. 동작 중인 broker는 교체하지 않는다.

```sh
uv run --directory services/agent-worker python ../../operations/subscription/install.py \
  --source-sha "$REVIEWED_SOURCE_SHA" --archive-sha256 "$ARCHIVE_SHA256" \
  --settings "$PRIVATE_METADATA_JSON" --instance-id "$DESIGNATED_INSTANCE_ID"
```

4. broker가 비활성인 작업 창에서 Terraform 계획을 적용한다. SSM association 성공과
   서비스의 STS endpoint 설정을 확인한 후 새 broker를 시작한다. broker 역할 ARN은
   `terraform output -raw broker_role_arn | gh secret set BRAVO_SUBSCRIPTION_BROKER_ROLE_ARN`
   으로 대상 저장소에 등록한다. SSM 설치 성공과 서비스 비활성을 확인한다. GitHub repository Secrets에는
   `BRAVO_SUBSCRIPTION_CONTROLLER_ROLE_ARN`, `BRAVO_SUBSCRIPTION_INSTANCE_ID` 참조만 넣는다.
   Variables에는 `BRAVO_SUBSCRIPTION_SOURCE_SHA`와 명시적 실행 허용
   `BRAVO_SUBSCRIPTION_RUNTIME_ENABLED=true`를 설정한다.
5. `subscription-runtime`을 dispatch한다. 실행은 실제 구독을 소비하므로 정상 PR CI와
   분리돼 있다. 설치 소스 SHA가 일치하지 않으면 결과를 수락하지 않는다. 실행이 끝나면
   터널과 broker를 종료하고 공개 가능한 판정/회전 여부만 artifact로 남긴다.

## T38 합성 검증과 T15 소비 경계

실행 기반의 담당·완료 기준은 [T38 #150](https://github.com/Softbank-Hackathon-Finals/platform/issues/150), 제품 템플릿·재시도·승인 전달은 [T15 #24](https://github.com/Softbank-Hackathon-Finals/platform/issues/24)에서 관리한다. 아래 합성 시험을 T15 제품 구현이나 전체 T38 통합 완료로 사용하지 않는다.

현재 연결된 템플릿은 `src/config.py`의 **합성 DB_HOST 환경변수 계약**이다.
실제 demo-chat의 Secrets Manager→OpenBao 수정과 SeaweedFS 정책 템플릿은 별도다.
실제 목적지 profile, 읽기 전용 credential 파일·회전 계약, 합성 시험 bucket/path가
준비되기 전에는 T15 전체 완료나 T9/T31/T30 통합 성공으로 사용하지 않는다.

controller는 요청마다 새 run ID를 보내고 broker는 `runs/<runId>/`를 한 번만 생성한다.
해당 bundle과 검증한 proposal을 읽기 전용 파일로 확정하므로 다음 요청이 덮어쓰지 않는다.
SSM verify는 그 파일과 요청한 run ID/proposal digest를 대조한 후에만 Job을 생성한다.
controller도 broker의 input/proposal digest와 receipt의 run/source/proposal/attempt,
PASS/effects_known, 실제 evidence digest와 정리·시험 결과를 대조해야 성공을 기록한다.

고정 source snapshot과 proposal digest를 다시 검증한 뒤 두 candidate Pod(환경값 있음/없음)와
별도의 verifier Job을 만든다. candidate는 숨긴 verifier 파일을 mount하지 않으며,
모든 Pod는 비루트·읽기 전용 루트·service-account token 없음으로 실행한다.
네트워크는 verifier→candidate의 HTTP 포트만 허용한다. image는 digest로 고정한다.
생성 Job UID, Pod의 controller owner UID, Pod UID, `run` 컨테이너의 imageID/실행 ID와
종료 상태를 확인한다. imageID는 요청한 digest와 같아야 하므로 실행 플랫폼의 manifest
digest를 사용한다. verifier의 고정 Pod와 `run` 컨테이너에서 로그를 읽은 뒤 candidate와
verifier의 동일성을 다시 확인한다. 소유권 불일치·재생성·복수 Pod·다른 이미지·재시작은
UNKNOWN이다. namespace는 원래 UID를 precondition으로 삭제한 뒤
부재까지 확인한다. cleanup 실패나 검증 불가이면 UNKNOWN/NEEDS_HUMAN이다.

PASS는 proposal/source/attempt/evidence digest가 일치하는 receipt로 승인 **대기**만 만든다.
사람의 변경 승인, T10 영속 저장, 승인 이미지 빌드·T31 배포를 대신하지 않는다.
이 어댑터는 최초 제안(attempt 1)만 받으며 실패하면 RETRY_ALLOWED를 반환한다.
최대 3회 규칙은 기존 Coordinator가 담당하지만 이 CLI는 자동 재시도를 실행하지 않는다.

### 합성 실행과 제품 입력

설치된 broker의 `POST /validation/fixture`는 고정 `examples/failure.json`, snapshot manifest, `examples/source`만 읽는다. 공식 `subscription-runtime` controller가 이 경로로 요청하며, 응답의 worker 결과와 VM에 보관한 `proposal.json`에는 `input_kind=synthetic_db_host_fixture`가 붙는다. 이 표기는 합성 DB_HOST 시험 결과의 출처다. controller는 이 값과 설치 소스 SHA, 실행 ID, 검증 영수증이 일치할 때만 합성 결과를 수락한다.

기본 설치의 `POST /run`은 HTTP 503과 `NEEDS_HUMAN / PRODUCT_INPUT_UNAVAILABLE`를 반환한다. 호출자가 bundle·snapshot·입력 종류를 보내도 자격 조회와 모델 실행 전에 종료한다. 제품 경로는 아래 호스트 연결을 명시적으로 제공해야 열린다. 기본 installer·공식 합성 workflow는 이 연결을 설치하지 않는다. 아래의 제품 모드 opt-in과 실제 인증된 T10 조회 서비스가 필요하다.

### 제품 제안 호스트 연결

`subscription_server.product_service(settings, inputs, journal_root)`와
`serve(settings, product=service)`는 인증된 제품 입력 조회기를 주입하는 호스트 전용
연결점이다. HTTP에는 `web_identity_token`, 불투명 `input_ref`, `input_digest` 세 필드만
허용한다. 제품 bundle이나 파일을 직접 제출할 수 없다. 기존 STS trust로 토큰을 검증한
뒤 STS가 돌려준 Provider·Audience·Subject의 digest를 조회기에 전달한다. 조회기가
거부하면 구독 Secret과 모델을 호출하지 않는다. 기존 600초·최대 2요청 창을 유지한다.

`ProductInputs.resolve(principal, input_ref)`의 호스트 구현은 호출자의 권한과 용도를
확인하고, **현재 커밋된 T10 Admission**을 승인된 논리 task·FailureBundle·SnapshotManifest·
파일에 결속해야 한다. `ProductInput.admission_digest`는 Admission 전체 필드에 기존
`bravo_agent.contracts.digest`를 적용한 값이고, bundle.run_id는 rehearsal_id다.
source_sha·attempt·tenant·plan/version·task_digest를 같은 정본에서 대조한다.
이 권한 확인을 단순 JSON 생성으로 대체하지 않는다. 만료·미커밋·미등록 입력은 거부한다.
요청의 input_digest는 파일을 포함한 전체 ProductInput의 canonical digest다.
T38은 이를 다시 계산하고 기존 T11 Snapshot의 경로·내용·크기·hash 검사와 제안 정책을 적용한다.

호스트의 `journal_root`는 소유자 전용 0700 영속 디렉터리다. SQLite FULL 동기화
트랜잭션으로 run/attempt를 **모델 호출 전** 점유한다. 같은 입력·호출자·설치 SHA의
재요청은 저장된 제안을 반환하며, 새 모델 호출을 하지 않는다. 입력 또는 설치 SHA가
바뀌면 거부한다. 중단 후 점유만 남은 경우는 `PRODUCT_OUTCOME_UNKNOWN`, 모델·정책
실패는 `PRODUCT_RUNTIME_UNCONFIRMED`로 멈춘다. journal 손실·복구 rollback을 새 실행
허가로 사용하지 않는다. 운영자는 기록과 중단 시 남은 비공개 staging 디렉터리를 조사한
뒤 별도로 인계해야 한다. journal은 broker만 쓸 수 있어야 하며 모델 프로세스와 입력
생산자에게 이 경로의 쓰기 권한을 주지 않는다.

성공 결과는 `input_kind=product`, `PROPOSAL_READY`, 제품 입력·Admission·snapshot·
proposal digest와 설치 SHA를 포함한다. T11의 원 작업 `task`와 `task_digest`도 결속하며,
최초 시도에서는 입력 bundle과 같아야 한다. 누락·불일치는 제안으로 수락하지 않는다.
이 `task_digest`는 T11 원 작업 bundle의 해시이며, T15 논리 작업의 해시인
T10 `Admission.task_digest`와 별도로 검증한다.
OIDC·AWS·구독 토큰과 임의 worker 진단 필드는
저장하지 않는다. 기록의 digest는 손상 탐지용이며 외부 서명이 아니다. T15의
`RecordedAttemptSender`·`AttemptStore`·`RecordedEvidence`가 검증 Job, PASS/FAIL,
생성/삭제 UID, 정리 및 승인 근거를 소유한다. 이 제안 기록만으로 PASS나
AWAITING_APPROVAL을 만들 수 없다. 현재 T11 CLI와 같은 최초 시도(attempt 1)만 실행하며
2·3회차는 호출 전에 거부한다. T11 상태 복원 계약 없이 번호를 1로 바꾸어 재시도하지 않는다.

제품 제안 응답은 `tool_events_status=UNAVAILABLE`를 명시한다. 현재 반환 경로는 T11
감사 배열 전체의 원문·순서·건수 결속을 전달하지 않는다. T15는 이를 빈 COMPLETE 배열로
바꾸지 않는다. 원격 제안 소비자가 T11의 완전한 배열을 인수한 뒤 T15 서명 기록과
T10 `data.tool_events`로 전달하는 접점을 연결해야 한다. 이 패치는 T24 수집을 구현하지 않는다.

호스트는 `bravo_agent.product_contracts`의 `ProductInput`, `ProductRequest`,
`validate_product_input`을 모델 런타임 import 없이 사용할 수 있다. 순수 입력 검사는
source/manifest/files의 일치·내용 hash·크기·자격 문자열을 대조하며 권한을 부여하지 않는다.
기존 `subscription_product` import도 같은 타입을 재export한다.

`subscription_product_host.RegisteredProductInputs(read)`는 T10의 인증된 현재 조회 포트를
broker의 `ProductInputs.resolve`로 연결한다. `read(principal, input_ref)`는
`admission`(T10 8필드), `product_input`, `input_digest`, `principal`,
`purpose="t38-product-proposal"`, `expires_at`(UTC RFC3339)을 반환한다. T10은 매번
커밋된 Admission·현재 plan/tenant/version/catalog·회수 여부를 검사한다. T38은 전체
Admission/입력 digest, 실행/source/회차, 호출자·용도·만료를 다시 대조한다. 저장된 사본이나
서명만으로 현재 권한 조회를 대신하지 않으며 조회 URL/함수를 HTTP 요청에서 받지 않는다.

`ProductTunnelCall(reviewed_runtime_sha)`는 T15의 `call(body) -> (status, response)` 포트다.
이미 수립된 전용 SSM TLS 터널의 `http://127.0.0.1:18762/run`만 호출하며 임의 endpoint,
환경 proxy, redirect, 자동 재시도를 허용하지 않는다. 응답 크기와 검토된 설치 SHA,
제품/task digest를 확인한 제안만 반환한다. T15는 자신의 등록 입력과 Admission/snapshot을
대조하고 기존 도구 계층으로 재검증한다. 응답 불명은 NEEDS_HUMAN이며 자동 새 호출을 하지 않는다.
이 함수는 터널 시작·토큰 발급·배포를 하지 않는다. 요청 토큰은 기존 GitHub OIDC 경로의 것이며
T10 workload JWT로 대체할 수 없다. 요청 `input_digest`는 ProductInput 전체이고, 응답의
`product_input_digest`가 그 값이다. 응답 `input_digest`/`task_digest`는 T11 FailureBundle이다.

### 제품 전용 설치와 호출 수명

`install.py --product-reader <metadata.json>`은 검토된 소스와 기존 설정 외에 정확히
`origin`(사설 HTTPS origin)과 `ca_file`(이미 마운트한 공개 CA의 절대 경로)만 받는다.
토큰·인증 키·업무 입력은 설정 필드가 아니다. 이 opt-in은 같은 hardening unit의 실행 모듈을
`bravo_agent.subscription_product_server`로 선택한다. 기본 설치는 제품 경로를 열지 않는다.
CA가 없거나 잘못되면 제품 host는 시작하지 않는다. 영속 `product-journal`은 설치기가
초기화하지 않으며, 제품 host에서는 합성 `/validation/fixture`를 404로 닫는다.

broker가 기존 STS에서 `/run` 토큰을 검증한 뒤에만 host의 요청별 `request_inputs(token)`을
만든다. 같은 토큰은 그 요청의 메모리에서 `ProductInputHTTPReader`에만 전달된다.
broker 프로세스가 운영자 설정 origin의 `POST /internal/v1/product-inputs/read`로
`{principal,input_ref}`를 보내며, Authorization에는 그 원 토큰을 사용한다. 이 경로는
T10의 별도 workload JWT 발급이나 VM 전달을 만들지 않는다. T10은 고정 GitHub issuer·
서명·audience·정확한 subject·immutable repository ID를 검증하고, 등록된 STS
Provider/Audience/Subject 조합과 본문 principal을 대조한 뒤 현재 plan/purpose 권한을 읽는다.
서명 검증만으로 입력 권한을 부여하지 않는다. STS Provider는 실측값을 운영자가 등록한다.

조회기는 지정 CA를 사용하고 proxy·redirect·재시도를 막는다. 응답 상한은 16 MiB다.
64개 파일의 UTF-8 바이트 상한에 JSON escaping이 더해지는 경우도 이 범위에서 수용하고,
등록본의 실제 schema·내용 상한은 별도로 검사한다. 토큰은 journal·ProductInput·worker에
들어가지 않는다. 오류에는 고정 사유만 반환한다.

`subscription_product_caller.ActionsProductToken()`은 실제 Actions 환경의 기존 OIDC
endpoint에서 `sts.amazonaws.com` audience로 요청당 토큰을 받는 주입 함수다.
`product_window(instance_id, reviewed_runtime_sha)`는 기존 controller 권한으로 고정
SSM start/tunnel/stop만 사용하며 `call(body)`를 한 번 허용한다. 응답 유실 뒤 다시 호출하면
거부한다. context를 닫으면 plugin·동일 SSM session·broker stop을 각각 처리하며,
어느 정리든 확인되지 않으면 성공으로 반환하지 않는다. SSM session 응답은
[고정 plugin 1.2.764.0의 환경 전달 계약](https://github.com/aws/session-manager-plugin/blob/1.2.764.0/src/sessionmanagerplugin/session/session.go#L156)을
사용해 argv에 토큰을 넣지 않는다. Actions bearer 환경도 plugin에 전달하지 않는다.

T10은 기존 durable dispatch를 점유한 뒤 이 call과 token supplier를 자신의 `request_for`에
주입한다. 등록/Admission·T15 Job·현재 권한·사람 승인은 여전히 기존 담당 포트가 소유한다.
Actions 밖의 상시 T10 host에서 이 supplier를 호출하면 실패한다. 이 경우 실제 Actions 실행과
T10 단일 dispatch를 연결할 검토된 orchestration 경로를 먼저 인수해야 하며 토큰 파일 복사로
대체하지 않는다. 이 모듈은 workflow를 생성하거나 기존 합성 controller를 제품 호출로 바꾸지 않는다.

실제 제품 실행 전에는 인증 입력 조회기의 배치·접근·수명, 영속 journal의 격리·보존,
T15의 원격 제안 소비 어댑터를 인수해야 한다. 목적지 API/RBAC와 credential 전달도 해당
담당의 별도 인수 대상이다. 로컬 가짜 provider 시험은 이 연결이나 실환경 인수를 증명하지 않는다.

교체 전에는 workflow 실행을 비활성화하고 진행 중인 broker와 터널이 정리될 때까지 기다린다. 같은 검토 commit의 VM broker와 GitHub controller를 배치하고 `BRAVO_SUBSCRIPTION_SOURCE_SHA`를 맞춘 뒤 합성 시험을 다시 연다. 새 controller가 구 broker를 호출하면 합성 전용 경로가 없어 실패하고, 구 controller가 새 broker를 호출하면 `/run`에서 503을 받는다. 구 버전 응답처럼 출처 표기가 빠진 경우에도 새 controller는 SSM verify를 접수하지 않는다. 어느 경우에도 다른 경로로 재시도하지 않는다. 교체 실패 시 workflow를 닫은 상태에서 일치하는 새 쌍을 복구한다. 구 쌍을 복원해야 하면 구 `/run`이 다시 합성 입력을 처리하므로 제품 호출은 계속 닫아 둔다.

### 제품 입력 인증만 확인하기

제품 host의 `POST /input/probe`는 `/run`과 같은 `ProductRequest`, STS 인증,
현재 입력 조회·digest·소스 검증을 사용한다. 성공은 `INPUT_AUTHENTICATED`와
요청/입력/Admission/주체 digest 및 설치 SHA만 반환한다. 모델 호출, 제안 claim,
소스 staging, T15 dispatch/검증 또는 승인 대기는 수행하지 않는다. 매 요청마다
현재 권한을 다시 읽으며 토큰·STS 자격·원문 입력은 응답에 포함하지 않는다.

Actions의 인수 코드에서는 `product_probe_window`와 기존 메모리 OIDC supplier를
사용할 수 있다. 이 창은 고정 SSM start/tunnel/stop과 한 요청 제한을 재사용한다.
제품 host의 기존 두 요청/600초 한도를 소비하며, probe 성공 뒤 실제 실행은
같은 Admission에 대한 T10의 별도 단일 dispatch 결정을 거쳐야 한다. probe 성공이
운영 배포·모델 제안·T15 통합 완료를 뜻하지 않는다. 실제 입력·origin/CA·배치가
준비되기 전 이 접점을 실행하지 않는다.

### 한 제품 실행을 시작하고 같은 결과 읽기

`subscription-product.yml`은 기존 합성 검증 workflow와 분리된 명시적
`workflow_dispatch`다. T10이 커밋한 같은 Admission/attempt 1의 불변
`ExecutionIntent` 일곱 필드(`request_id`, `input_ref`, `input_digest`,
`admission_digest`, `runtime_source_sha`, `mode`, `recipient_public_key`)만 받는다. mode는 인증만 하는
`probe` 또는 제안 한 번인 `propose`이며 JWT·업무 원문·임의 주소는 받지 않는다.

T10은 intent를 커밋하기 전에 운영자 `client.recipient(request_id)`가 반환한 실행별
공개 수신키를 함께 결속한다. 복호키는 운영자의 같은 state-dir 안에 원자적으로
0600으로 저장하며 GitHub/Pod에 전달하지 않는다. 동시 준비·재시작은 같은 키를
재사용하고, 발행 뒤 키가 없어지면 UNKNOWN으로 멈추며 새 키/실행으로 복구하지 않는다.
이 키는 결과 암호화 전용이며 AWS·GitHub 인증 자격이 아니다.

T10의 실행 claim과 운영 입력을 준비한 **기존 운영자**가 자신의 `gh` 인증으로
`OperatorProductExecution(state_dir).submit(intent)`를 한 번 호출한다. 재시작,
응답 유실 또는 같은 호출은 `read(intent)`와 같은 조회만 수행한다. T10 Pod에
GitHub 자격을 설치하거나 새 background dispatcher를 만드는 경로가 아니다.
다른 호스트나 새 state directory로 실행 기록을 우회해서 다시 보내지 않는다.

```sh
# intent commit 전에 공개키만 준비한다. state-dir는 같은 영속 운영자 디렉터리다.
uv run --frozen python -m bravo_agent.subscription_product_dispatch recipient \
  --request-id execution-id --state-dir .private/product-executions
# intent.json에는 위 일곱 metadata 필드만 둔다.
uv run --frozen python -m bravo_agent.subscription_product_dispatch submit \
  --intent intent.json --state-dir .private/product-executions --output receipt.json
# 결과가 아직 없거나 응답이 불명이면 같은 intent/state-dir를 조회한다.
uv run --frozen python -m bravo_agent.subscription_product_dispatch read \
  --intent intent.json --state-dir .private/product-executions --output receipt.json
```

운영자는 publish 전에 지정 VM에 설치된 SHA와 현재 develop/workflow SHA가 같고,
인증된 T10 read·현재 권한·목적지가 준비됐는지 확인한다. workflow는 정확 저장소
ID·develop·workflow SHA·첫 실행만 허용하고, 기존 VM concurrency/고정 SSM 창에서
한 요청 후 정리한다. workflow rerun은 자격 취득 전에 거부한다. `propose` 성공은
모델 제안까지만 의미하며 T15 검증·승인 대기를 대신하지 않는다.

발행기는 네트워크 요청 전에 intent와 발행 시각을 영속 기록한다. 같은 요청의
복수 run, 다른 actor/workflow/SHA, 두 번째 run attempt, 실패·정리 불명은 UNKNOWN이다.
결과가 진행 중이면 `ExecutionPending`, 확정할 수 없으면 `ExecutionUnknown`이며
CLI는 각각 4·5로 끝난다. 자동 재발행하지 않는다.

공개 저장소 artifact의 read access를 제품 원문 접근 권한으로 취급하지 않는다.
Actions는 [X25519 + HKDF](https://cryptography.io/en/50.0.2/hazmat/primitives/asymmetric/x25519/)와
AES-256-GCM으로 결과를 암호화하며, 새 ephemeral key/nonce와 같은 intent·수신키·run/attempt를
AAD로 결속한다. 평문 결과를 runner 파일이나 로그에 쓰지 않는다. 같은 run의
`subscription-product-result` artifact 한 개에서 암호문만 읽고 운영자 메모리에서 복호한다.
송신자의 신뢰는 암호화만이 아니라 정확 GitHub run/workflow/SHA 출처 검증에 둔다.
[GitHub artifact API](https://docs.github.com/en/rest/actions/artifacts)의 run/SHA와
SHA-256을 대조하고, 제한된 ZIP의 `product-result.json`을 추출 없이 읽는다. 순수
`product_execution_contracts.validate_execution_receipt`는 입력·Admission·설치·실행
결속 및 정리 성공을 검사한다. T10은 소비 직전 현재 권한·plan/catalog/만료를 다시
검사하고, 기존 T15 소비 경로로 전달해 실제 Job과 원본 영수증을 확인한다. 인증
probe와 모델 제안의 실행 ID/결과를 섞지 않는다. CLI 출력·artifact에는 토큰·STS
자격을 보존하지 않는다. 복호된 결과는 제품 제안이므로 운영 입력의 기존 접근 범위로 취급하고 공개 문서나
로그에 복사하지 않는다. CLI의 명시적 결과 파일은 운영자 로컬 0600으로만 생성한다.
실행 원장과 복호키는 원본 결과 인수·보존 정책에 맞춰 함께 관리하며, 키를 삭제하면
남은 artifact로 복구할 수 없다는 점을 확인한 뒤 정리한다.

### 플랫폼 T10과 기존 운영자 호스트 사이의 한 번 전달

T10 DB/Coordinator는 플랫폼의 기존 runtime 자격 mount와 같은 프로세스에 둔다.
`gh` 로그인과 결과 복호 개인키는 기존 운영자 호스트에 남긴다.
`bravo_agent.product_execution_mailbox.FileProductExecution`은 T10의
`recipient/submit/read` client 포트를 전용 파일 인계로 연결한다. AWS/GitHub 인증,
plan 승인, 새 스케줄러 또는 상시 네트워크 서버를 추가하지 않는다.

배치 담당은 실제 T10 UID 소유 0700 디렉터리 한 곳을 같은 Admission/attempt 1에
배정한다. 일반 API 요청·모델·SSM upload 본문으로 디렉터리나 Pod를 선택하지 않는다.
공유 실행 창에서 기존 SSO의 계정/역할 ID, 플랫폼 instance, 대상 namespace/Pod UID,
설치 SHA와 실제 프로세스 UID를 대조한 뒤 해당 경로만 인수한다. T10 담당의 읽기 전용
준비 조회가 반환한 여섯 필드에 위 운영자 `recipient(request_id)` 공개키를 붙인다.
새 request_id를 임의로 만들어 기존 durable intent를 우회하지 않는다.

| 파일 | 누가 기록하는가 | 의미 |
|---|---|---|
| `prepared.json` | 인증된 운영자, T10 UID로 실행 | 전체 일곱 필드의 사전 준비. 실행 시작 아님 |
| `intent.json` | T10 `submit`, DB intent 커밋 이후 | 같은 준비값을 불변 게시한 실행 요청 |
| `receipt.json` | 인증된 운영자, T10 UID로 실행 | GitHub 출처·암호문·정리까지 확인한 동일 실행 결과 |

모든 파일은 0600이며 원자적 게시 후 fsync한다. 같은 값은 다시 읽고 다른 값은
덮어쓰지 않는다. 심볼릭 링크·FIFO·다른 UID·공개 파일·크기 초과·교체 디렉터리를 거부한다.
`recipient`는 plan 잠금 안에서 로컬 준비 파일만 읽는다. `submit` 뒤 결과가 없으면
Pending, intent 자체가 없거나 결속이 다르면 Unknown이다. `read`는 intent를 만들지
않으며, 재시작·응답 유실을 새 dispatch로 복구하지 않는다. 결과 수신은 여전히 T10의
현재 권한 재검사와 T15의 검증을 통과해야 한다.

실행 창의 전달 순서는 다음과 같다. 아래 명령은 각 호스트의 검토된 agent-worker
환경에서 실행한다. `$MAILBOX_DIR`는 배치 담당이 지정한 T10 전용 경로,
`$REMOTE_PORT`/`$LOCAL_PORT`는 그 창에만 할당한 loopback 포트다.

1. 기존 SSO/SSM으로 확인한 정확 Pod/UID에서 `prepare`를 실행한다. stdin에는 공개
   metadata만 넣는다. T10 host에는 이 디렉터리의 `FileProductExecution`을 주입한다.

   ```sh
   python -m bravo_agent.product_mailbox_transfer prepare --directory "$MAILBOX_DIR" < intent.json
   ```

2. T10이 현재 권한 검사와 DB claim을 마치고 `submit`한 뒤, 같은 Pod/UID에서
   `claimed`를 읽는다. 이 명령의 출력은 공개 intent 필드뿐이다. 준비 파일만 있고
   게시된 intent가 없으면 중단한다. 받은 일곱 필드가 사전 준비 원문과 같을 때만
   운영자 호스트의 기존 `subscription_product_dispatch submit/read`를 사용한다.

   ```sh
   python -m bravo_agent.product_mailbox_transfer claimed --directory "$MAILBOX_DIR"
   ```

3. provider 검증·복호가 끝난 receipt를 운영자 로컬 0600 파일로 보관한다.
   `product_execution_mailbox.encode(receipt)`의 canonical bytes 길이·SHA-256만
   SSM 명령에 전달해 같은 T10 Pod에서 수신기를 시작한다. 결과 본문·JWT·개인키는
   SSM 명령/argv/출력으로 전달하지 않는다. 최대 1 MiB·120초·PUT 한 건만 받는다.
   수신기는 `127.0.0.1`에만 bind하며 ready metadata 뒤 종료 영수증만 출력한다.

   ```sh
   python -m bravo_agent.product_mailbox_transfer receive --directory "$MAILBOX_DIR" \
     --port "$REMOTE_PORT" --sha256 "$RECEIPT_SHA256" --bytes "$RECEIPT_BYTES" --seconds 120
   ```

4. 기존 창 담당이 그 Pod UID를 다시 확인하고 loopback port-forward와 기존 SSM
   포트 터널을 연다. 이 포트를 Service/Ingress/공개 주소로 노출하지 않는다.
   receiver의 기대 해시는 인증된 SSM 제어 경로에서 정하며 HTTP 입력이 바꾸지 못한다.
   operator upload는 환경 프록시·redirect를 사용하지 않고 loopback으로만 전송한다.

   ```sh
   uv run --frozen python -m bravo_agent.product_mailbox_transfer upload \
     --intent intent.json --receipt receipt.json --port "$LOCAL_PORT"
   ```

5. 같은 Pod/UID의 `receipt-status`에서 request_id·hash·size를 다시 읽는다. 출력에
   제품 원문은 없다. 업로드 응답이 유실돼도 같은 receipt 상태부터 읽고 workflow를
   재발행하지 않는다. T10은 파일을 읽고 현재 권한을 재검사한다. 창 담당은 자신이
   시작한 receiver PID/시작 신원, port-forward와 SSM session을 정리하고 listener
   부재를 확인한다. 영속 intent/receipt는 결과 인수 전 임의로 삭제하지 않는다.

   ```sh
   python -m bravo_agent.product_mailbox_transfer receipt-status --directory "$MAILBOX_DIR"
   ```

이 코드는 전달 도구다. 실제 Pod/디렉터리 배치, SSO/SSM 창과 TLS, 운영 등록 입력,
T15 token-free 소비 경로가 준비됐다는 증거는 아니다. 동일 DB/Coordinator/AttemptStore
프로세스를 운영자 호스트로 옮기거나 기존 runtime 자격을 복사해 맞추지 않는다.
