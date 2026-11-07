# SOVA Console (데스크톱) · T21

SOVA 이전 운영을 한 화면에서 보는 데스크톱 콘솔이다. 대시보드와 안내자(에이전트)를 겸하며 **SOVA MCP를 기본 탑재**한다.
구성은 Codex 데스크톱 앱의 배포 번들을 REA한 결과(Electron·React·Tailwind v4·Radix·cmdk·jotai·TanStack Query, 입력창·
세션 관리 메시지 체계)를 따르고, 화면은 SOVA 대표 이미지의 색·로고·키치함을 쓴다.

| 부분 | 하는 일 |
|---|---|
| `src/main/sova.ts` | 루프백 프록시. 요청마다 AWS SSO STS 증명(audience `sova-mcp`)을 붙여 플랫폼 MCP로 보낸다. 등록·계획 생성의 **확인 호출은 사람이 확인 창에서 누를 때까지** 보류한다 |
| `src/main/agent.ts` | 안내자. 앱에 내장한 OpenAI Codex 런타임(`@openai/codex-sdk`)을 사용자의 **ChatGPT 계정**으로 실행한다. 셸·앱·플러그인·브라우저·개인 MCP는 끄고 SOVA MCP만 연다 |
| `src/main/proof.ts` | STS 서명 증명(60초). 자격 값은 저장하지 않는다 |
| `src/renderer` | 대시보드(소바면 단계 레일·이벤트·다음 행동·관측), 계획·타임라인·관측/보안·처음 안내·설정, 안내자 패널(모델·추론·Fast, 승인 방식, `/` 프롬프트·`@` 언급, 대기열·조정, 작업 요약 접기, 찾기, 메시지 탐색), 세션 관리(고정·정렬·검색·읽지 않음·분기·보관·삭제) |

## 실행

```sh
pnpm install
pnpm start          # 개발 실행
pnpm lint && pnpm test && pnpm build
pnpm make           # macOS 앱(DMG/ZIP). Codex 런타임을 Resources/codex-runtime으로 내장
```

처음 실행하면 연결 화면에서 SOVA MCP 주소와 AWS SSO 프로필을 넣는다. 안내자는 설정 › 안내자 모델에서 ChatGPT로 로그인한다.
승인·실행·복귀·정리는 이 앱의 안내자가 할 수 없다. 사람이 결정한다.

로컬 시험: `services/sova-mcp`에서 `uv run python tests/dev_server.py`(합성 데이터, 배포 아님). 루프백 주소이면 상단에 "로컬 시험 데이터"가 표시된다.
