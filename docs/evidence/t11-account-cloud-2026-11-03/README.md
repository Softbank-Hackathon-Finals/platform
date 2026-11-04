# T11 구독 계정의 AWS 실행 검증

2026-11-03, 기존 T7 비공개 RKE2 노드의 별도 비루트 프로세스에서 **공식 구독 인증 → 실제 `gpt-6-astra` 응답 → 승인된 소스 읽기 → 정책을 통과한 제안서 제출**을 완료했다. 최종 v9 결과는 `PROPOSAL_READY`, 종료 코드 0, 보고된 사용량 12,974토큰이다. 입력은 합성 실패·소스 fixture이며 운영 앱의 리허설이나 배포 완료를 의미하지 않는다. 개인 계정, 기기 코드, 토큰과 운영 자원 식별자는 공개하지 않는다.

| 확인 대상 | 최종 관측 |
|---|---|
| AWS 구독 인증 | 공식 device login 완료, `SUBSCRIPTION_AUTHENTICATED` |
| 모델·도구 호출 | `repo.read`와 `plan.propose` 허용, 모델 `PROPOSAL_COLLECTED` |
| 제안 결속 | run/source/input/snapshot/proposal digest 대조 통과 |
| 제안 내용 | 합성 `src/config.py`의 DB host를 주입 환경에서 읽도록 제안 |
| 후속 로컬 검사 | Python 문법, 주입 host 사용, host 미지정 시 실패 확인 |
| 코드 검증 | 로컬·AWS Ruff 및 pytest **91개 통과**, 실행 입력 **22개 SHA-256 일치** |
| 인증·수명 | 인증 파일 미생성, 실행 프로세스 0개, 전용 임시 설치·실행 디렉터리 회수 |
| 제외 범위 | T15 실제 Job·인증된 영수증, 사람 승인, T31 배포 및 팀 공통 인증 등록은 별도 |

[최종 v9 결과](current-account-v9.json), [소스 manifest](source-manifest-v9.json), [AWS 검사](cloud-checks-v9.json), [실행 후 해시·프로세스 검사](runtime-verification-v9.json), [정리 영수증](cleanup-v9.json)에 관측 시각·SSM 명령 ID의 SHA-256·비공개 원본의 해시를 남겼다. 공개 JSON은 필요한 결과 필드를 선택한 사본이며 원시 SSM 로그가 아니다. 모델 실행 코드의 커밋은 `21daa7da754f1d0a31da22f13f06dffbb6dc78c0`이다. `008708c`까지의 문서·ignore·기준 브랜치 통합은 이 22개 실행 입력을 바꾸지 않았다. 그 뒤 추가하는 SIWC 관리자 연결은 별도 코드·검증 범위다.

## 발견한 문제와 수정

1. 이전 [한도 초과 기록](account-cloud-result.json)은 브라우저에 먼저 선택되어 있던 다른 계정의 결과다. 현재 Codex 운영자 계정의 남은 사용량으로 해석하면 안 된다. 이번 v7~v9에서 초기화권이나 추가 크레딧을 사용하지 않았다.
2. [v7](current-account-v7.json)은 인증과 실제 모델 사용량 8,280토큰을 확인했지만 제안서가 없었다. 로컬 실호출 비교에서 `code_mode_host=false`가 동적 도구 전달도 막는 것을 재현했다. 호스트만 켜고 모델의 Code Mode·셸·내장 실행 도구는 계속 껐다.
3. [v8](current-account-v8.json)은 설정 수정 후에도 AWS에서 제안이 없었다(8,308토큰). 공식 standalone 배포의 `codex-code-mode-host`가 설치되지 않은 것을 확인했다. 같은 0.157.0 릴리스의 보조 파일을 해시 검증해 설치하고, 로그인 전 설치 검사와 회귀 시험을 추가했다. 설치 누락이면 인증·모델 프로세스 시작 전에 `ACCOUNT_INSTALLATION_INCOMPLETE`로 종료한다.
4. v9의 최초 업로드 검사는 import 정렬 오류로 모델 호출 전에 멈췄다. 정렬을 고친 최종 소스는 AWS에서 Ruff·91개 시험과 같은 격리 조건의 보조 실행 파일 기동 검사를 통과했다. 그 뒤 새 인증으로 v9 실호출이 성공했다.

자유 텍스트나 사용량만으로 성공 처리하지 않는다. 원시 모델 텍스트는 감사 기록에 복사하지 않고, 허용된 도구가 제출한 제안만 채택한다. 제안된 코드는 운영 파일에 적용하지 않았다. 후속 로컬 검사는 T15의 독립 검증 영수증이 아니다.

## 실행 경계

- 이번 결과는 사용자가 직접 요청한 운영자 구독 진단이다. 일반 Responses API 키나 팀 공통 무인 인증의 성공 증거로 바꾸지 않는다. 팀 구독 연동은 앱 소유의 공식 Sign in with ChatGPT 등록·갱신 경로로 별도 검증한다.
- 공식 Codex 0.157.0 CLI와 보조 실행 파일을 사용했다. 배포 압축 파일과 압축 해제한 실행 파일의 해시는 서로 다르며 [실행 후 검사](runtime-verification-v9.json)에 구분했다. CLI 압축 파일 SHA-256은 `db3fe3adaa35c50edfb68a988a117782fe3492960fb63d7003eb6748ccc0657b`, 보조 파일 압축 SHA-256은 `47d32419e895c9cddbb664a2890bfb250ef0e3fded3da70e5f2bc61479f1989c`다.
- [시작 조건](execution-isolation-v9.json)은 `DynamicUser`, `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, 메모리 1 GiB·CPU 1개·15분 제한이다. 모델에는 환경 접근 없이 두 정책 도구만 등록했다. Kubernetes Job·CNI 송신 정책 실측과는 구분한다.
- 인증은 메모리 전용이며 로그인 직후와 제안 종료 후 `auth.json` 부재를 검사했다. 인증의 비영속성 때문에 진단을 재실행할 때마다 사용자 인증이 필요했다. 운영자 로그인 파일을 복사하거나 팀 공통 자격으로 게시하지 않았다.
- [정리](cleanup-v9.json) 후 기존 RKE2가 계속 active임을 확인했다. 기존 EC2·T7 인프라는 소유 세션의 작업을 위해 유지했다. 앞선 `cleanup.json`은 이전 실행의 기록이며 최종 v9 정리와 구분한다.

## T7의 별도 실측

[Actions 실행 41602534226](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41602534226)과 [읽기 영수증](t7-oidc-readback.json)은 OIDC→고정 SSM→RKE2 ConfigMap 배포와 Secret 조회·다른 namespace 쓰기·임의 셸 문서 실행 거부를 확인한 이전 실측이다. 이 증거만으로 플랫폼 DB·앱 이미지 전달·Executor 통합이나 T7 전체 완료를 주장하지 않는다.

이 폴더의 기존 [계정 검증 기록](account-cloud-result.json)의 `final_source_test`에 당시 pytest 87개와 Ruff 통과 결과가 있다. `source-manifest.json`, `execution-isolation.json`, `cleanup.json`도 같은 시기의 기록이다. 현재 결과는 `*-v9.*` 파일을 따른다.
