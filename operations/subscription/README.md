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
broker는 역할을 받을 때 같은 토큰에 더해 지정 VM의 사설 IPv4와 전용 STS interface endpoint를 요구한다.
Terraform이 VM과 같은 서브넷에 단일 AZ endpoint를 만들고 SSM으로 broker 서비스에만
`AWS_ENDPOINT_URL_STS`를 설정한다. VM 교체 시 이 스택을 다시 적용한다.
endpoint의 사설 DNS는 꺼져 있으므로 같은 VPC의 다른 서비스는 기존 STS 경로를 쓴다.
이 VM은 RKE2 노드이기도 하다. Canal이 Pod 트래픽을 노드 IP로 변환하면 보안 그룹만으로
Pod와 broker를 구분할 수 없다. SSM이 endpoint ENI의 IPv4를 읽어 호스트 nftables에
전용 테이블 하나를 설치한다. 이 주소의 TCP 443에 한해 Pod 전달 패킷을 차단하고
호스트에서 시작한 연결은 broker의 systemd slice에만 허용한다. 표준 Linux 네트워크를
사용하는 이 지정 노드가 대상이다. 호스트 root·관리자 또는 다른 데이터 경로까지
격리한다는 뜻은 아니다. 일반 STS, CNI 규칙, 노드 역할은 변경하지 않는다.

운영 확인: `bravo-sts-guard.service`가 active인지와 `nft list table inet bravo_broker_sts`로
endpoint 전용 규칙·카운터를 확인한다. broker slice에서 TCP가 열리고 일반 slice·Pod에서
막혀야 한다. 같은 Pod에서 별도 허용 목적지에 연결돼야 차단 시험이 유효하다.
재부팅 시 guard를 시작하며 broker 시작은 guard 성공을 요구한다. VM 또는 endpoint 교체
후에는 Terraform을 적용하고 이 확인을 반복한다. 공유 노드를 남기고 스택을 철거할 때는
실행을 멈추고 broker의 IAM trust 및 endpoint를 먼저 제거한 뒤 guard를 disable하고
이 스택의 `bravo_broker_sts` 테이블, guard unit·script, slice unit과 broker의
`sts.conf`만 제거하고 `systemctl daemon-reload`한다.

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

일반 `POST /run`은 HTTP 503과 `NEEDS_HUMAN / PRODUCT_INPUT_UNAVAILABLE`를 반환한다. 호출자가 bundle·snapshot·입력 종류를 보내도 자격 조회와 모델 실행 전에 종료한다. T11의 `FailureBundle`·`SnapshotManifest`는 검증 형식을 제공하고 T15의 `T11BucketProposer`는 주입한 coordinator를 소비한다. 현재 보호된 생산자가 제품 계획의 run ID·소스 commit·snapshot을 이 VM에 전달하는 경로는 없다. 이 연결을 마련한 뒤 제품 검증을 인수한다.

교체 전에는 workflow 실행을 비활성화하고 진행 중인 broker와 터널이 정리될 때까지 기다린다. 같은 검토 commit의 VM broker와 GitHub controller를 배치하고 `BRAVO_SUBSCRIPTION_SOURCE_SHA`를 맞춘 뒤 합성 시험을 다시 연다. 새 controller가 구 broker를 호출하면 합성 전용 경로가 없어 실패하고, 구 controller가 새 broker를 호출하면 `/run`에서 503을 받는다. 구 버전 응답처럼 출처 표기가 빠진 경우에도 새 controller는 SSM verify를 접수하지 않는다. 어느 경우에도 다른 경로로 재시도하지 않는다. 교체 실패 시 workflow를 닫은 상태에서 일치하는 새 쌍을 복구한다. 구 쌍을 복원해야 하면 구 `/run`이 다시 합성 입력을 처리하므로 제품 호출은 계속 닫아 둔다.
