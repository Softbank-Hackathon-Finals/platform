# T41 기존 인증 자동 큐 등록 인수 시험

새 봇 계정이나 키 없이 기존 Actions의 `PROJECT_TOKEN`으로 준비된 일반 PR을 GitHub 기본 Merge Queue에 등록하는지 확인한다. 구현은 [PR #261](https://github.com/Softbank-Hackathon-Finals/platform/pull/261), 작업과 실제 결과는 [T41 #262](https://github.com/Softbank-Hackathon-Finals/platform/issues/262)에서 추적한다.

## 시험 순서

1. 이 문서만 추가한 Draft PR에서 필수 CI가 성공해도 큐에 들어가지 않는지 확인한다.
2. Ready로 변경한 뒤 승인·수동 큐 등록 없이 현재 head가 자동 등록되는지 확인한다.
3. 실제 `merge_group` CI의 성공과 최종 병합 SHA를 연결한다.

이 문서의 추가 자체는 시험 성공을 뜻하지 않는다. 결과는 실제 workflow run과 GitHub 상태를 읽어 기록한다. 가짜 status, 관리자 우회, 보호 규칙 완화는 사용하지 않는다. 기존 수정 요청 PR의 보류, main 정책과 sync PR 수동 등록은 유지한다.
