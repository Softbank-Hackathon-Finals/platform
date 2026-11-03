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

`SessionDocumentAccessCheck`를 강제하여 DocumentName을 생략한 기본 shell도 거부한다.
워크플로는 임의 shell 명령, 기본 shell session, 임의 port session, Secret 목록 조회의
거부를 실제 호출로 확인한다. 허용된 SSM 문서는 start/stop/verify enum만 받고 사용자
명령·포트·manifest 입력은 받지 않는다. `GetCommandInvocation`은 AWS에서 리소스 수준
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

4. SSM 설치 성공과 서비스 비활성을 확인한다. GitHub repository Secrets에는
   `BRAVO_SUBSCRIPTION_CONTROLLER_ROLE_ARN`, `BRAVO_SUBSCRIPTION_INSTANCE_ID` 참조만 넣는다.
   Variables에는 `BRAVO_SUBSCRIPTION_SOURCE_SHA`와 명시적 실행 허용
   `BRAVO_SUBSCRIPTION_RUNTIME_ENABLED=true`를 설정한다.
5. `subscription-runtime`을 dispatch한다. 실행은 실제 구독을 소비하므로 정상 PR CI와
   분리돼 있다. 설치 소스 SHA가 일치하지 않으면 결과를 수락하지 않는다. 실행이 끝나면
   터널과 broker를 종료하고 공개 가능한 판정/회전 여부만 artifact로 남긴다.

## T15 검증 범위

현재 연결된 템플릿은 `src/config.py`의 **합성 DB_HOST 환경변수 계약**이다.
실제 demo-chat의 Secrets Manager→OpenBao 수정과 SeaweedFS 정책 템플릿은 별도다.
실제 목적지 profile, 읽기 전용 credential 파일·회전 계약, 합성 시험 bucket/path가
준비되기 전에는 T15 전체 완료나 T9/T31/T30 통합 성공으로 사용하지 않는다.

고정 source snapshot과 proposal digest를 다시 검증한 뒤 두 candidate Pod(환경값 있음/없음)와
별도의 verifier Job을 만든다. candidate는 숨긴 verifier 파일을 mount하지 않으며,
모든 Pod는 비루트·읽기 전용 루트·service-account token 없음으로 실행한다.
네트워크는 verifier→candidate의 HTTP 포트만 허용한다. image는 digest로 고정한다.
검증 Job UID가 바뀌면 결과를 거부하고, namespace는 원래 UID를 precondition으로 삭제한 뒤
부재까지 확인한다. cleanup 실패나 검증 불가이면 UNKNOWN/NEEDS_HUMAN이다.

PASS는 proposal/source/attempt/evidence digest가 일치하는 receipt로 승인 **대기**만 만든다.
사람의 변경 승인, T10 영속 저장, 승인 이미지 빌드·T31 배포를 대신하지 않는다.
이 어댑터는 최초 제안(attempt 1)만 받으며 실패하면 RETRY_ALLOWED를 반환한다.
최대 3회 규칙은 기존 Coordinator가 담당하지만 이 CLI는 자동 재시도를 실행하지 않는다.
