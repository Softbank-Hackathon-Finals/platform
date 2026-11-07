# [ADR-0038] SOVA MCP는 T10 저장소를 함께 쓰는 별도 플랫폼 호스트로, 사람 전용 효과 없이 연다

* **상태 (Status):** 채택됨(Accepted). 2026-11-07 T21 담당(사용자 지시: "SOVA MCP를 계획대로 최종 완성, 플랫폼 RKE2에 이미지로").
* **날짜 (Date):** 2026-11-07
* **관련 (Related):** T21 #27, T10, Notion "SOVA API · MCP 설계 명세 (초안 v0.1)", ADR-0019, ADR-0031

## 1. 배경 및 문제 상황

* 명세는 콘솔·CLI·AI 클라이언트가 같은 API·같은 권한 검사를 쓰고, MCP는 T10 호스트 안의 얇은 어댑터가 되며, 승인·실행은 사람이 콘솔에서 한다고 정했다. 위치·scope·사람 전용 범위는 ADR로 정하기로 남겨 두었다.
* 현재 T10은 등록·조회·워크플로를 서로 다른 프로세스로 나눠 같은 PostgreSQL 저장소를 쓴다. 사람 신원은 AWS SSO의 STS `GetCallerIdentity` 서명 증명(60초)을 audience별로 검증한다.

## 2. 결정

1. **위치:** `services/sova-mcp`를 T10 등록 호스트 옆의 별도 Deployment(`platform-state/sova-mcp`)로 둔다. 같은 저장소의 `PlanStore`·`Registry`(`registration_server.build_registry`)를 직접 호출한다. 새 상태 머신·게이트·실행기를 만들지 않는다. 플랫폼 RKE2에만 배치한다.
2. **신원:** `SsoRegistrationVerifier`를 사람마다 고정하고 audience를 `sova-mcp`로 둔다. 등록·운영자·콘솔 증명은 MCP에서, MCP 증명은 그쪽에서 거부된다. 검증된 증명은 수명(≤60초) 동안만 캐시한다. 받은 토큰을 뒤쪽 API로 넘기지 않는다. 관측·보안 API는 호스트가 가진 읽기 자격으로만 호출하고, 테넌트의 등록 환경에 매핑된 것만 읽는다.
3. **도구:** 읽기 도구는 항상 연다. 상태를 만드는 도구(환경·앱 등록, 계획 생성)는 `read_only=false` 연결에서만 보이고, 첫 호출은 미리보기와 HMAC 확인 토큰(주체·도구·정확한 입력·5분)을, 같은 입력·토큰의 두 번째 호출만 T10 `Registry.register`를 부른다. 같은 토큰은 같은 등록을 재생한다. elicitation 대신 이 2단계를 쓴다(무상태 HTTP와 모든 클라이언트에서 동작).
4. **사람 전용:** 승인·승인 철회·컷오버/복귀 실행·reconcile·SAFE_HOLD·Edge 재시도·정리·리허설 시작은 MCP 도구로 열지 않는다. `sova_request_approval`과 `next_actions`는 콘솔 링크만 돌려준다.
5. **진입:** tailnet 고정 이름(`sova-mcp`)의 Tailscale Service. 기존 운영자 태그·grant(admin/owner → tcp:443)를 재사용한다. 새 태그·grant는 T44 tailnet 조각 생성 경로에서 정한다.
6. **게시:** `publish-sova-mcp-image`가 develop 커밋만 빌드·스캔해 GHCR에 올리고, 차트는 digest만 받는다.

## 3. 결과

* 명세 P0(읽기·맥락·안내·이벤트 polling·대조)와 P1의 등록·계획 접수가 같은 T10 검사 아래 열린다. v2 자원(runtime profiles·assessments·operations)은 develop에 없어 도구를 만들지 않았다.
* 남은 일: 워크로드 등록은 승인 checkout(등록 호스트의 init 컨테이너)이 없으면 거부된다. 관측(T37 #705)·보안 발견 API가 배포되면 upstream 설정만 채운다. 팀원 전원 접근은 T44 grant 확장 후다. 확인 키는 프로세스별이라 replica는 1이다.
