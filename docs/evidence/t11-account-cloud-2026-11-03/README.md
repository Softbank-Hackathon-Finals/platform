# T11 구독 계정의 AWS 실행 검증

2026-11-03, 기존 T7의 비공개 RKE2 노드에서 공식 Codex CLI 0.157.0을 별도 비루트 프로세스로 실행했다. **ChatGPT 구독 장치 인증은 성공했고 실제 모델 요청은 제공자의 `usageLimitExceeded`로 거부됐다.** 성공한 모델 응답이나 제안서는 아직 없다. 계정 식별자·인증 코드·토큰·운영 자원 주소는 공개 증빙에서 제외했다.

| 확인 대상 | 결과 |
|---|---|
| AWS → 공식 구독 인증 | `SUBSCRIPTION_AUTHENTICATED`, 장치 로그인 성공 |
| 인증 보관 | CLI `ephemeral` 저장소, 실행 중 `auth.json` 미생성, 종료 시 프로세스 그룹 회수 |
| AWS → 실제 모델 요청 | `gpt-6-astra` 요청 후 `usageLimitExceeded` 수신 |
| T11 결과 | `NEEDS_HUMAN`, 제안서 없음, 사용량 수치 없음 |
| 최종 수정 코드 | 로컬·AWS에서 Ruff와 pytest 87개 통과, 실행 입력 22개 SHA-256 일치 |
| 독립 검증·운영 반영 | T15·승인·배포를 실행하지 않음 |

[계정 검증 결과](account-cloud-result.json)는 실호출 시점의 진단 버전과 이후 최종 코드 시험을 구분한다. [소스 manifest](source-manifest.json)는 최종 코드의 커밋과 파일 해시다. 최종 버전의 성공한 모델 응답을 얻기 전에는 전체 모델 경로를 검증 완료로 표시하지 않는다.

## 실행 경계와 수정

- 사용자 요청에 따라 개인 운영자 계정을 사용한 일회성 검증이다. 기본 워커의 Responses API 인증을 검증한 것으로 보지 않으며, 구독 인증을 팀용 무인 서비스로 제공하지 않는다. 공식 [원격 장치 인증](https://learn.chatgpt.com/docs/auth#login-on-headless-devices)과 [app-server 인증 범위](https://learn.chatgpt.com/docs/app-server#auth-endpoints)를 따른다.
- 바이너리는 공식 릴리스의 x86_64 Linux musl 배포본이며 SHA-256은 `db3fe3adaa35c50edfb68a988a117782fe3492960fb63d7003eb6748ccc0657b`다. 버전이 다르면 검증기를 중단한다.
- AWS 실행에는 `DynamicUser`, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, 메모리 1 GiB·CPU 1개·15분 제한을 적용했다. 모델에는 환경 접근을 주지 않고 두 정책 도구만 등록했다. Kubernetes Job·CNI 송신 정책 검증은 별도다.
- 처음 실행은 로그 출력 디렉터리가 준비되지 않아 시작 전에 중단됐다. 실행 디렉터리를 먼저 만든 뒤 재실행했다.
- 초기 제안 실행의 일반 오류를 조사해 구독 한도 초과를 확인했다. 최종 코드에서는 허용된 오류 분류와 HTTP 상태만 기록하고 원문은 기록하지 않는다. 프로토콜 응답보다 먼저 도착한 알림을 버리지 않도록 보완했고, 사용량 초과·잘못된 제안·예상 밖 실행·이벤트 순서에 대한 회귀 시험을 추가했다.
- 한도 초과를 확인한 뒤 모델 대체·자동 재호출·추가 크레딧 구입을 하지 않았다. 사용량 초기화는 별도 사용자 결정이다.

## T7의 별도 실측

[GitHub Actions 실행 41602534226](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41602534226)의 OIDC 세션과 AWS SSM 영수증을 다시 조회했다. [T7 읽기 결과](t7-oidc-readback.json)에 기록한 RKE2 준비, 고정 명령을 통한 ConfigMap 배포, Secret 조회·다른 namespace 쓰기·임의 셸 문서 실행 거부는 성공했다.

이는 플랫폼 DB, GHCR 앱 이미지의 digest push/pull, 수집·Executor 역할 통합 또는 지속 운영 환경 전체의 완료 증거가 아니다. 이번 T11 검증은 기존 노드를 사용했으며 새 EC2·NAT·VPC를 만들지 않았다.
