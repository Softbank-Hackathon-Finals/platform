# ADR-0005: 리뷰 승인 후 머지

* **상태:** 팀장 지시 반영 — 2026-11-03, 이 문서 PR은 리뷰 대기
* **관련:** T29, #10, ADR-0004

## 배경

기존 작업 안내와 봇 워크플로는 CI 통과 뒤 자동 머지하도록 되어 있었다. 팀장이 리뷰를 받은 뒤 머지하도록 명시했다. CI는 코드·설정 검사를 수행하며 팀원의 검토와 승인을 대신하지 않는다.

## 결정

- main과 develop 모두 PR과 기존 필수 검사 3개에 더해 팀원 리뷰 승인 1건 이상을 요구한다.
- 코드를 추가하면 기존 승인을 해제하고 최종 커밋에 다시 승인을 받는다. 변경 요청도 해소한 뒤 머지한다.
- 조건을 충족할 때 병합되도록 GitHub 기본 auto-merge를 사용할 수 있다. 에이전트·봇이 승인이나 필수 검사를 우회하지 않으며, 전체 리뷰 후보의 승인을 기다리도록 확장하지 않는다.
- 승인과 필수 검사를 확인한 HEAD를 지정해 merge commit으로 합친다. 보호 규칙을 우회하지 않는다.
- 봇이 만든 체크·담당 동기화 PR에도 같은 리뷰 조건을 적용한다.

## 결과와 검증

main/develop 보호 규칙의 required_approving_review_count를 1, dismiss_stale_reviews_on_push를 true로 설정하고 API로 재조회했다. 필수 검사, 삭제·force push 제한, 우회 권한 없음은 유지했다. 문서와 워크플로도 같은 조건으로 맞춘다.

이슈와 보드는 먼저 갱신될 수 있지만 로컬 tasks.md는 봇 PR의 리뷰·머지 뒤 따라온다. 이 차이를 완료 여부와 혼동하지 않는다. 이미 머지한 변경은 이 결정으로 자동 되돌리지 않는다.

## 후속 운영 합의 범위 — 11/3 16:18 KST

팀원이 1인 승인 조건의 변경 의도를 물었고, 팀장은 의도한 변경임을 알렸다. 이어 **불편하거나 개발·병합 병목이 되면 1인 승인 필수 조건은 없애도 된다**고 안내했다. 필수 CI를 유지하며 필요한 변경에 리뷰를 요청하는 방식도 허용했다. [Slack 안내](https://softbankhackathon2026.slack.com/archives/C0CDEFEB83U/p1793690297841029)

이 안내 자체로 실제 ruleset을 변경하지는 않았다. 위 1인 승인 조건은 현재 설정이며 영구 불변 규칙이 아니다. 팀이 변경하면 API로 실제 ruleset을 다시 읽고 작업 안내·봇·리뷰 도구의 설명도 함께 정렬한다. 승인 수를 별도 코드에 중복 하드코딩하지 않는다.

## 리뷰 이벤트 실행 경계

PR #73의 병합 전 `pull_request_review` 실행에서 새 workflow가 `prs`를 호출했지만, checkout은 아직 그 명령이 없는 develop을 가져와 종료 코드 2로 실패했다. [실패 실행](https://github.com/Softbank-Hackathon-Finals/platform/actions/runs/41602467983). 이는 검사 실패를 허용할 사유가 아니라 workflow와 실행 코드의 버전 경계 문제다.

리뷰 이벤트는 권한 없는 `pr-review-signal`로 받고 실제 동기화는 기본 브랜치의 `workflow_run`이 담당한다. 신호 workflow는 저장소 코드·시크릿·산출물을 사용하지 않는다. 동기화 코드는 workflow 자체의 불변 SHA로 checkout하며 현재 PR 상태를 API에서 조회한다. 기존 PR/이슈/예약 경로와 직렬화·dry-run·오류 전파를 유지한다. 미병합 코드를 권한 있는 실행에 넣거나 `prs` 오류를 무시하는 호환 분기는 추가하지 않는다.

[GitHub 이벤트 규칙](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run)과 [workflow SHA 정의](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts#github-context)를 따른다. 로컬 검사와 브랜치 지정 미리보기는 가능하지만, 실제 리뷰 신호 → 기본 브랜치 동기화는 두 workflow가 병합된 뒤 검증한다.
