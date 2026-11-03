# 관리자 구독 연결과 자격증명 주입

`bravo-subscription`은 플랫폼 운영자가 자신의 ChatGPT 구독 사용을 **Bravo 앱에 직접 동의**하는 관리 명령이다. 비밀번호를 받거나 개인 Codex의 `auth.json`을 복사하지 않는다. 현재 범위는 팀이 직접 운영하는 단일 VM이다. 일반 사용자가 호출하는 공개 인증 서버나 유료 고객용 호스팅 통합을 제공하지 않는다.

## 등록과 실행

1. 지정 VM의 인증 담당 사용자로 `init-host`를 실행한다. 출력되는 UUID는 비밀이 아닌 호스트 결속 정보다. 같은 VM의 실행·갱신·회수는 모두 이 상태 디렉터리를 사용한다. 파일은 0600, 디렉터리는 0700이며 한 VM·한 인스턴스만 운영한다.

   ```sh
   bravo-subscription init-host --state-dir "$BROKER_STATE_DIR"
   ```

2. 관리자 자신의 AWS 로그인으로 팀 Secret을 한 번 생성한다. 이 명령은 토큰 없는 `UNCONFIGURED` 레코드만 저장한다. 반환된 ARN과 위의 UUID를 비공개 운영 설정에 보관한다. 이름·ARN·계정·이메일은 소스에 하드코딩하지 않는다. 토큰은 Terraform 입력·state·파일·CI 로그에 넣지 않는다.

   ```sh
   bravo-subscription init-secret \
     --name bravo-platform/agent-subscription/primary \
     --region "$AWS_REGION" --runtime-host-id "$RUNTIME_HOST_ID"
   ```

3. 브라우저가 있는 **로컬 관리자 기기**에서 로그인한다. 로컬 상태 디렉터리는 VM의 것과 별개다. loopback 리스너를 먼저 열고 공식 인증 페이지로 이동한다. PKCE·state·nonce와 서명된 ID의 issuer/audience/계정을 검증하고, 앱이 발급받은 토큰 묶음을 AWS TLS API로 직접 Secrets에 저장한다. 토큰 파일을 만들지 않는다.

   ```sh
   bravo-subscription login \
     --secret-arn "$SUBSCRIPTION_SECRET_ARN" \
     --runtime-host-id "$RUNTIME_HOST_ID" --state-dir "$LOCAL_ADMIN_STATE_DIR"
   ```

   자동 브라우저 실행을 대신할 때만 `--authorization-file /private/path/login-url`을 쓴다. 이 0600 임시 파일에는 PKCE 인증 시작 URL만 있고 토큰은 없다. URL을 로그·채팅에 붙이지 않으며 종료 시 삭제된다. 개인 브라우저에서 계정과 구독 사용 동의를 확인한다. 이미 `ACTIVE`인 등록에 `login`을 반복하면 새 인증을 열지 않는다. 다른 계정으로 덮어쓰기도 금지한다. 계정 교체는 실행을 멈추고 기존 세션을 회수한 뒤 별도 Secret/등록으로 관리한다.

4. `status`로 연결 상태와 계정을 확인한다. 이 출력의 이메일은 관리자용 개인정보이므로 공개 증빙에 복사하지 않는다. VM에서 `models`로 현재 구독의 모델 목록을 조회한 뒤 그중 모델 하나를 명시해 `run`을 수행한다.

   ```sh
   bravo-subscription status --secret-arn "$SUBSCRIPTION_SECRET_ARN" \
     --runtime-host-id "$RUNTIME_HOST_ID"
   bravo-subscription models --secret-arn "$SUBSCRIPTION_SECRET_ARN" \
     --runtime-host-id "$RUNTIME_HOST_ID" --state-dir "$BROKER_STATE_DIR"
   bravo-subscription run --secret-arn "$SUBSCRIPTION_SECRET_ARN" \
     --runtime-host-id "$RUNTIME_HOST_ID" --state-dir "$BROKER_STATE_DIR" \
     --model "$MODEL_ID" --bundle /private/path/failure.json \
     --manifest /private/path/snapshot-manifest.json \
     --snapshot /private/path/read-only-snapshot --output /private/path/new-proposal.json
   ```

모델 자식 프로세스에는 `ACCESS_TOKEN` 하나와 제한된 실행 환경만 전달한다. 갱신/ID 토큰·AWS·GitHub·다른 모델 API 자격과 사용자 홈은 전달하지 않는다. `PROPOSAL_READY`는 제안 수집이며 T15 리허설 PASS나 사람 승인이 아니다. 자식 stdout/stderr와 제공자 원문 오류는 관리자 로그로 복사하지 않는다.

## AWS 권한과 배치 경계

사람은 각자 AWS 계정/SSO, CI는 검토된 OIDC, 운영 인증 프로세스는 전용 workload identity를 사용한다. 장기 액세스 키를 공유하지 않는다. 정확한 Secret ARN에 필요한 정책은 다음 명령으로 생성한다.

```sh
bravo-subscription policy --secret-arn "$SUBSCRIPTION_SECRET_ARN"
```

이 정책은 해당 항목의 `GetSecretValue`, `PutSecretValue`, `UpdateSecretVersionStage`만 허용한다. Secret 생성 권한은 관리자 초기 준비 단계에만 필요하다. 사용자 지정 KMS 키를 선택하는 배치는 그 키의 최소 권한을 별도로 검토한다. 기본 명령은 Secrets Manager 기본 암호화를 쓴다. Secret ARN/역할 참조는 비공개 배치 설정에 두고 토큰 값을 GitHub Secret에 중복 복사하지 않는다.

**현재 T7 smoke OIDC 역할과 노드의 명시적 시크릿 거부 정책은 이 PR에서 바꾸지 않는다.** 전용 인증 프로세스의 workload identity·실행 사용자·Secret 참조를 T7 소유자가 결속해야 운영 자동 실행이 연결된다. `run` 명령만으로 이 배치가 설치됐다고 간주하지 않는다.

환경 허용 목록은 OS 격리를 대신하지 않는다. 배치에서는 모델 프로세스의 클라우드 metadata/자격 파일/인증 프로세스 메모리 접근을 차단하고, 읽기 전용 앱 snapshot·출력 디렉터리·필요한 모델 송신만 제공해야 한다. 모델에는 명령 실행 도구가 없고 승인된 읽기·제안만 제공한다. 인증 프로세스와 리허설 guest/운영 DB 권한을 분리한다. 상태 디렉터리는 보호된 로컬 파일시스템에 두며 NFS·공유 호스트 ID·여러 replica를 지원한다고 주장하지 않는다.

## 갱신·동시 실행·회수

- 접근 토큰의 잔여 시간이 5분 이하이면 지정 VM이 갱신한다. 읽기·갱신·실행·회수 전체를 같은 비차단 파일 잠금으로 직렬화한다. 다른 실행은 `SUBSCRIPTION_BROKER_BUSY`로 멈춘다. 토큰은 메모리와 Secrets에만 존재한다.
- 갱신 토큰이 회전하면 전체 자격을 새 버전에 먼저 쓰고, 기존 버전이 아직 `AWSCURRENT`인 경우에만 현재 포인터를 이동한다. 실패 시 모델을 시작하지 않는다. 새 pending 버전은 관리자 복구용으로 보존하며 이전 토큰으로 자동 재시도하지 않는다. 버전 포인터가 바뀐 상태에서 다른 세션의 등록을 덮어쓰지 않는다.
- terminal refresh 오류는 토큰을 현재 레코드에서 제거하고 `REAUTH_REQUIRED`로 표시한다. 네트워크·일시 제공자 오류는 자격을 지우지 않는다. 구독 권한이 없으면 검증된 계정/client ID 결속만 유지한 `PLAN_DISABLED`로 저장한다. 이후 운영자가 명시적으로 `login`을 실행할 때만 동의를 다시 요청한다.
- 회수는 지정 VM의 잠금 아래서 공식 discovery의 동일 issuer revoke endpoint를 호출한다. 원격 200을 확인한 뒤 현재 레코드를 `SIGNED_OUT`으로 교체한다. 원격 회수가 확인되지 않으면 완료라고 표시하지 않는다.

  ```sh
  bravo-subscription revoke --secret-arn "$SUBSCRIPTION_SECRET_ARN" \
    --runtime-host-id "$RUNTIME_HOST_ID" --state-dir "$BROKER_STATE_DIR"
  ```

Secrets Manager 이전 버전은 회수 명령으로 물리 삭제되지 않는다. IAM 접근을 제한하고 원격 세션을 회수하며, 보관 종료 시 팀의 Secret 삭제/보존 절차를 따른다. 앱 연결 취소는 ChatGPT 설정에서도 할 수 있다. 제공자는 취소 알림을 실시간 전송하지 않으므로 다음 호출/갱신에서 확인할 수 있다.

## 사용량과 검증

구독은 무제한 사용을 뜻하지 않는다. 이 경로는 `store=false`, `stream=true`, 이름공간으로 제한한 두 함수 도구를 사용한다. 완료 이벤트 전의 텍스트나 부분 응답을 성공으로 채택하지 않는다. 한도·권한·지역·지원 기능 오류는 한 번의 실행을 중단한다. 자동 재로그인, 초기화권 사용, 크레딧 구매, API 키 fallback은 없다. 모델을 바꾸는 것도 운영자의 명시 선택이다.

로컬 시험은 실제 SDK와 모의 OAuth/JWKS·Secrets·SSE 응답으로 계정 결속, 토큰 교체, 동시 실행 거부, 환경 분리, 한도·중단 실패를 확인한다. 실제 검증은 **초기 등록 → Secrets 저장 → 모델 목록 → 실제 구독 제안 → 다음 실행의 로그인 재사용 → 갱신/회수**를 별도 근거로 기록한다. 기존 [AWS v9 Codex 진단](../../docs/evidence/t11-account-cloud-2026-11-03/README.md)은 새 SIWC 경로의 완료 증거가 아니다.

공식 근거: [SIWC 등록](https://developers.openai.com/siwc/token-sharing-open-source/sign-in), [자체 호스팅 VM](https://developers.openai.com/siwc/token-sharing-open-source/self-hosted-vms), [세션 갱신·회수](https://developers.openai.com/siwc/token-sharing-open-source/profiles-and-sessions), [구독 모델 호출](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference), [오류와 복구](https://developers.openai.com/siwc/token-sharing-open-source/errors-and-recovery).
