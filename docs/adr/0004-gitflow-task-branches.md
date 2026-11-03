# ADR-0004: Gitflow와 작업 번호를 분리한 브랜치

* **상태:** 채택됨 — 팀장 요청, 2026-11-03
* **관련:** T29, #10

## 배경

기존 문서·자동화는 기능 브랜치에서 main으로 바로 합쳤다. 11월 3일 Slack에서 develop을 두는 흐름을 논의했고, 팀장이 `feature/`와 `T번호`를 구별하고 Gitflow를 따르도록 요청했다. 여러 담당의 작업을 통합한 상태와 릴리스 상태를 구분해야 한다.

## 검토한 선택

- 기존 main 중심 PR: 단계가 적지만 통합 중인 변경과 릴리스 범위를 같은 브랜치에 둔다.
- develop을 둔 Gitflow: 통합·릴리스가 구별되며 release/hotfix를 양쪽에 반영하는 절차가 필요하다.

## 결정

`feature/T번호-설명 → develop`, `release/vX.Y.Z → main + develop`, `hotfix/T번호-설명 → main + develop`을 따른다. 커밋·PR의 `[T번호]`는 유지한다. 기능은 merge commit으로 묶는다. 자세한 절차는 [개발 안내](../development.md)에 둔다.

## 결과

GitHub develop 보호 규칙에 main과 같은 필수 검사 3개를 적용한다. main 보호 규칙은 기본 브랜치가 바뀌어도 main을 계속 가리키도록 명시한다. CI는 두 장기 브랜치를 검사하고, 작업 동기화는 develop을 읽고 쓴다. 진행 중인 다른 담당의 코드·체크박스를 이 결정으로 수정하지 않는다.

이 결정은 작업 흐름 변경이며 제품 아키텍처·시나리오·판정 기준을 바꾸지 않는다.

리뷰와 머지 조건은 [ADR-0005](0005-review-before-merge.md)에서 구체화한다.
