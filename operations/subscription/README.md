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

기본 설치의 `POST /run`은 HTTP 503과 `NEEDS_HUMAN / PRODUCT_INPUT_UNAVAILABLE`를 반환한다. 호출자가 bundle·snapshot·입력 종류를 보내도 자격 조회와 모델 실행 전에 종료한다. 제품 경로는 아래 호스트 연결을 명시적으로 제공해야 열린다. 현재 CLI·installer·공식 workflow는 이 연결을 설치하지 않는다.

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

실제 제품 실행 전에는 인증 입력 조회기의 배치·접근·수명, 영속 journal의 격리·보존,
T15의 원격 제안 소비 어댑터를 인수해야 한다. 목적지 API/RBAC와 credential 전달도 해당
담당의 별도 인수 대상이다. 로컬 가짜 provider 시험은 이 연결이나 실환경 인수를 증명하지 않는다.

교체 전에는 workflow 실행을 비활성화하고 진행 중인 broker와 터널이 정리될 때까지 기다린다. 같은 검토 commit의 VM broker와 GitHub controller를 배치하고 `BRAVO_SUBSCRIPTION_SOURCE_SHA`를 맞춘 뒤 합성 시험을 다시 연다. 새 controller가 구 broker를 호출하면 합성 전용 경로가 없어 실패하고, 구 controller가 새 broker를 호출하면 `/run`에서 503을 받는다. 구 버전 응답처럼 출처 표기가 빠진 경우에도 새 controller는 SSM verify를 접수하지 않는다. 어느 경우에도 다른 경로로 재시도하지 않는다. 교체 실패 시 workflow를 닫은 상태에서 일치하는 새 쌍을 복구한다. 구 쌍을 복원해야 하면 구 `/run`이 다시 합성 입력을 처리하므로 제품 호출은 계속 닫아 둔다.
