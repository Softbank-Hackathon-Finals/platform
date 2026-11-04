# T11 관리자 구독 연결과 AWS 재사용 검증

2026-11-03, **Bravo 앱 소유 SIWC 등록 → AWS Secrets Manager 저장 → 전용 역할로 조회 → AWS 비루트 모델 프로세스 → 실제 제안 제출**을 두 번 확인했다. 두 번째 실행은 같은 등록과 Secret 버전을 사용했고 새 로그인 없이 성공했다. 개인 Codex 캐시를 복사하거나 API 키로 대체하지 않았다.

| 검사 | 관측 결과 |
|---|---|
| 관리자 연결 | 요청한 계정 일치, 서명·nonce·issuer·audience·앱 client·구독 scope 검증, `ACTIVE` |
| 실제 Secrets 저장 | 생성, 전체 레코드 쓰기, 이전 버전에 결속한 AWSCURRENT 교체, 재조회 일치 |
| 전용 AWS 역할 | 정확한 Secret의 Get/Put/UpdateVersionStage만 허용; 자체 항목 조회 성공·전체 Secret 목록 조회 거부 |
| 첫 AWS 실행 | `gpt-6.1-sol`, 2회 Responses 요청, 1,493토큰, `PROPOSAL_READY`, exit 0 |
| 두 번째 AWS 실행 | 같은 등록·같은 Secret 버전, 2회 요청, 1,543토큰, `PROPOSAL_READY`, exit 0. 로그인 없음 |
| 실제 도구 | 두 실행 모두 `repo.read ALLOWED` → `plan.propose ALLOWED` → `PROPOSAL_COLLECTED` |
| 코드 검증 | 로컬·AWS 각각 177 tests, Ruff 통과. 정책 관련 회귀 51개 포함 |
| 소스 결속 | `bce854c73447c6967306ddafb58ef00fa6ad70be`, 29개 실행·시험 입력 해시 AWS 재조회 일치 |
| 정리 | 검증 프로세스 종료, 암호화 전달 세션 종료, 임시 설치/결과 삭제. 기존 RKE2 active 유지 |

[등록·권한 결과](connection.json), [첫 실행](run-1.json), [재실행](run-2.json), [AWS 검사](cloud-checks.json), [소스 manifest](source-manifest.json), [정리 결과](cleanup.json)에 확인 범위와 원본 해시를 남겼다. 공개본에는 이메일·계정 번호·Secret/역할/호스트 식별자·토큰·인증 URL을 넣지 않는다. SSM 명령 식별자는 해시로만 남기고 원본 영수증은 비공개로 보관한다.

## 실제 주입 방식과 보존 항목

이번 검증에서는 운영자가 전용 역할의 1시간 STS 세션을 발급하고 [AWS Session Manager의 암호화 포트 전달](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html)로 AWS의 10분 한정 인증 프로세스 메모리에 주입했다. 명령 인자·SSM 명령 본문·디스크 파일에 자격 값을 넣지 않았다. 인증 프로세스가 Secrets를 읽고 모델 자식에는 접근 토큰 하나만 전달했다. 자식에게 갱신/ID 토큰이나 AWS 자격을 전달하지 않았다.

AWS 프로세스는 DynamicUser, 읽기 전용 시스템, 보호된 홈, private tmp/devices, 권한 상승 금지, 메모리·CPU·시간 제한을 적용했고 EC2 metadata 주소 접근을 막았다. 이 구성과 도구 제한이 임의 코드에 대한 완전한 격리를 증명한다는 뜻은 아니다. 이번 입력은 합성 fixture이고 모델에게 명령 실행 도구를 제공하지 않았다.

사용자가 요청한 재사용을 위해 **앱 등록/세션이 든 Secret, 정확한 항목만 허용하는 관리자용 역할, 지정 VM의 호스트 ID**는 보존했다. 영구 키나 상시 daemon을 만들지 않았다. 기존 T7 노드의 시크릿 거부 정책과 smoke OIDC 역할은 변경하지 않았다. 관리자가 직접 시작하는 이번 검증과, 운영 workload identity를 연결한 상시 무인 배치는 구분한다.

## 발견한 문제와 수정

- 계정 모델 목록은 655,945바이트였다. 128 KiB 인증 응답 한도를 재사용해 거부하던 문제를 `093e44f`에서 고쳤다. 모델 목록에만 2 MiB 한도를 두고 인증/JWKS의 작은 한도는 유지했다. 이미 저장된 구독을 재사용해 수정 후 검증했으며 다시 로그인하지 않았다.
- 리뷰의 wildcard 권한 지적은 `bce854c`에 반영했다. JSON Statement의 Effect를 구분해 Deny 정책을 허용하고 Allow 배열 뒤쪽/Principal 객체의 전체 허용을 거부한다.
- systemd DynamicUser의 StateDirectory 루트는 관리되는 심볼릭 링크일 수 있다. 보호된 경로 아래 실제 `state/` 디렉터리를 사용해 호스트 결속 파일을 생성했다. 심볼릭 링크 자체를 허용하도록 경로 검사를 완화하지 않았다.

## 아직 입증하지 않은 범위

- **실제 토큰 갱신/원격 회수:** 등록 직후 제공자가 지정한 갱신 가능 시각이 미래였다. 이를 우회해 조기 갱신하지 않았다. 갱신·회전·충돌·terminal/일시 오류·회수 실패 처리는 모의 제공자 시험으로 확인했다. 실제 회수는 유지해야 할 사용자 연결을 끊으므로 수행하지 않았다.
- **상시 무인 운영:** 전용 역할은 현재 운영자만 신뢰한다. T7의 지속 실행 workload identity·배치·관측 결속은 별도다. 무인 운영에 개인 장기 AWS 키를 넣는 방식으로 대체하지 않는다.
- **제안의 독립 검증:** 두 결과 모두 주입된 `DB_HOST`를 사용함을 로컬 합성 실행으로 확인했다. 첫 결과는 환경값이 없으면 localhost를 유지했고 두 번째는 누락 시 실패했다. 이것은 모델 제안이 의미상 동일하거나 운영 승인됐다는 증거가 아니다. T15의 비공개 시험·인증된 receipt·승인·T31 반영은 아직 수행하지 않았고 앱 원본도 변경하지 않았다.
- 구독 사용량은 총 3,036토큰이었다. 초기화권·추가 크레딧·유료 API fallback을 사용하지 않았다. 구독 한도와 앱별 제한이 없는 것으로 해석하지 않는다.
