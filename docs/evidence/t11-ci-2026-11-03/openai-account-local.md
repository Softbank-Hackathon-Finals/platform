# 기존 ChatGPT 계정으로 수행한 로컬 모델 호출

2026-11-03 16:11 KST, 사용자의 기존 ChatGPT 로그인과 공식 Codex CLI 0.157.0으로 **실제 OpenAI 모델 응답을 1회 받아 T11 제안서 스키마와 정책 핸들러에 통과시켰다.** 원시 로그·인증 설정은 비공개로 보존하고, 공개본에는 [값을 최소화한 영수증](openai-account-local.json)만 둔다.

| 경계 | 확인한 결과 |
|---|---|
| 인증 | 공식 CLI의 기존 ChatGPT 로그인 사용. OAuth 값을 읽거나 다른 환경으로 복사하지 않음 |
| 모델 | 사용자 계정에서 선택한 `gpt-6-astra`, 낮은 reasoning effort |
| 입력 | 저장소 합성 예제의 `DB_HOST="localhost"` 실패. 운영 데이터 없음 |
| 응답 | 환경 변수 주입을 사용하는 단일 파일 수정 제안. 구조화 출력 유효 |
| 사용량 | 입력 16,612토큰, 출력 147토큰. 계정 한도 사용이며 API 청구액 측정이 아님 |
| 도구 | CLI 실행 이벤트에서 모델 도구 실행 0건. controller가 승인된 snapshot을 읽고 결과를 기존 정책 핸들러로 제출 |
| 조정기 | `PROPOSAL_READY`. T15 독립 검증과 승인·운영 반영은 실행하지 않음 |

## 실행 조건과 재현 기준

`codex login status`로 기존 ChatGPT 로그인을 확인한 뒤 `codex exec`의 `--ignore-user-config`, `--ignore-rules`, `--strict-config`, `--ephemeral`, `--sandbox read-only`, `--output-schema`를 사용했다. 별도 비공개 실행 디렉터리에서 웹 검색·shell·MCP·멀티에이전트·apps·plugins·hooks를 비활성화하고 180초 timeout을 두었다. 모델에 저장소 또는 운영 자격증명을 전달하지 않았다. 입력 소스는 기존 `Snapshot`/`Tools`로 읽고, 응답은 `Proposal`로 검증한 뒤 기존 `plan_propose`와 `Coordinator`에 전달했다.

처음 시도는 내장 provider의 재시도 설정 override가 허용되지 않아 모델 호출 전에 실패했다. 그 설정을 제거한 실행에서 `turn.completed`와 제안서를 받았다. 수집 harness가 개발 중 기능 경고를 예상 밖 이벤트로 분류했으므로, 재호출 없이 같은 로그의 종료 이벤트·사용량·응답·정책 결과를 다시 대조해 영수증을 만들었다. 원시 경고를 숨기지 않고 영수증에 요약했다.

호스트 스킬 로더 경고가 관측됐다. 따라서 이 실행은 **로컬 운영자 환경의 계정 인증 검증**이며, 플랫폼의 완전히 분리된 실행환경 검증으로 확대하지 않는다. AWS 컨테이너 검증은 별도 [기록](README.md)에 있다.

## 플랫폼 인증과 구분

공식 Codex 문서는 저장된 CLI 인증을 비대화형 실행에서 사용할 수 있다고 설명하지만, 계정 인증을 공개 저장소 CI로 옮기는 방식을 권장하지 않는다. 따라서 개인 OAuth를 GitHub Secrets, Actions, AWS 컨테이너에 복사하지 않았다. [공식 비대화형 실행 문서](https://learn.chatgpt.com/docs/non-interactive-mode)

플랫폼의 현재 `OpenAIProvider`는 Responses API 인증을 사용한다. 이 실행은 그 API transport의 실호출 검증이 아니며, API 키 또는 별도 공식 계정 연동을 아직 연결하지 않았다. 공식 Sign in with ChatGPT 연동도 애플리케이션 소유 인증 경로와 지원 파라미터 검토가 필요하므로 CLI 토큰을 그대로 대입하지 않는다. [공식 계정 연동 경계](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server), [지원 제한](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)

계정 구독 한도와 API 사용 비용은 별개다. 이번 결과를 모델 사용료 0원 또는 AWS 월 50만원 예산으로 보호되는 플랫폼 운영 비용이라고 표현하지 않는다. T11 전체 완료·T15 PASS·클라우드 모델 egress 검증은 여전히 별도다.
