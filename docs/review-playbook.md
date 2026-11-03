# PR 리뷰와 수정 근거

이 절차는 여러 PR의 현재 상태를 수집하고, 사람이 판단한 내용과 검증 근거를 남기는 데 쓴다. GitHub가 병합 규칙을 집행한다. 수집기와 LLM은 승인하거나 병합 가능 여부를 대신 판정하지 않는다.

현재 협업 원칙은 **해당 파트의 주·보조 담당자에게 리뷰를 요청하고, 적격한 사람 한 명 이상이 승인하면 필요한 CI와 변경 요청 처리 상태를 확인한 뒤 병합**하는 것이다. 초대한 리뷰어 전원이 승인할 때까지 기다리는 방식은 아니다. 기존 문서의 ‘리뷰 없이 CI만으로 병합’ 설명과 충돌하면 현재 사용자의 이 지시를 우선한다. 실제 적용 규칙은 GitHub의 대상 브랜치에서 매번 확인한다.

## 1. 여러 PR의 현재 상태 수집

저장소 루트에서 실행한다. Python 표준 라이브러리와 로그인된 `gh`가 필요하다. 기존 인증을 사용하며 토큰 값을 인자로 전달하지 않는다.

```sh
python3 scripts/review_packet.py 73 74 \
  --repo Softbank-Hackathon-Finals/platform \
  --output-dir /absolute/path/to/private-review-packets
```

PR별로 시각이 붙은 새 디렉터리에 `packet.json`과 `packet.md`를 생성한다. 이전 결과를 덮어쓰지 않는다. 출력은 기본적으로 저장소 밖에 둔다. 파일이 들어가는 새 디렉터리는 본인만 접근하도록 생성한다.

| 수집 항목 | 읽는 근거 | 해석 한계 |
|---|---|---|
| head/base SHA, 대상 브랜치, draft, reviewDecision, mergeStateStatus, auto-merge | GitHub PR GraphQL, 수집 시작·끝 재조회 | API 호출 사이의 상태는 원자적이지 않다. 행동 직전에 재조회한다. |
| 변경 파일과 T 번호 | 페이지를 모두 읽은 변경 파일, PR 제목·본문·브랜치의 T 번호 | T 번호 언급이 실제 담당 범위라는 뜻은 아니다. diff와 역할표를 비교한다. |
| 설계·태스크·개발 규칙 | base/head SHA에 고정한 문서 링크 | 링크 생성은 문서 검토를 수행했다는 뜻이 아니다. |
| 승인과 변경 요청 | 전체 리뷰 이력, 현재 GitHub reviewDecision | 사람의 현재 head 승인만 별도로 세며, 작성자·봇·이전 head 승인을 제외한다. GitHub의 최종 결정과 구분한다. 승인자의 저장소 권한 자격은 이 도구가 계산하지 않는다. |
| 필수 CI | 대상 브랜치의 effective rulesets, exact-head check runs/statuses | classic branch protection은 이 API의 범위 밖이다. 누락된 필수 정책이 없는지 GitHub에서 확인한다. |
| 스레드와 코멘트 원문 링크 | 전체 review thread, inline comment, discussion, review 페이지 | 스레드의 resolved 값은 승인이나 수정 검증이 아니다. 원문을 직접 읽는다. |

Exit 0과 `capture_status: COMPLETE`는 요청한 자료 수집이 완료됐다는 뜻이다. CI 실패나 리뷰 미승인 PR도 정상적으로 수집할 수 있다. `required_ci: PASS`는 **수집한 ruleset의 필수 context**에 대해 현재 SHA의 성공 근거를 확인했다는 좁은 의미다. 병합 허가는 아니다. check와 legacy status가 같은 이름이면 둘 다 보여 주며, 실패·취소·누락·다른 SHA를 성공으로 바꾸지 않는다. `neutral`과 `skipped`도 이 도구에서는 성공으로 올리지 않는다. 앱이 제한된 legacy status의 발급 앱을 입증할 수 없으면 `NOT_VERIFIED`로 남긴다.

API 실패, 권한 부족, 파싱 오류, 페이지 제한 도달, 불완전한 목록, 수집 도중 revision 변경은 `UNKNOWN`과 exit 1을 남긴다. 여러 PR 중 하나가 실패해도 나머지 결과를 보존하며 전체 명령은 실패한다. 페이지는 API별 최대 30개, 요청은 각각 최대 45초다. 한도에 걸린 자료를 일부만 읽고 완료로 표시하지 않는다. `UNKNOWN`을 승인이나 통과로 해석하지 않는다.

수집기는 PR 본문·코멘트 본문·diff patch·CI 원시 로그를 저장하지 않는다. 원문은 링크에서 읽는다. 제목, 파일 경로, 리뷰어, 상태, 규칙 및 근거 URL은 저장되므로 패킷도 프로젝트의 비공개 자료로 취급한다.

## 2. 기준 문서와 실제 변경 비교

PR의 base에서 팀이 채택한 계약을 먼저 읽고, head에서 무엇이 달라지는지 확인한다. 로컬 diff를 볼 때는 GitHub PR과 같은 공통 조상 기준인 `git diff <base-sha>...<head-sha>`를 사용한다. 보통 `AGENTS.md`, `docs/design.html`, `docs/tasks.md`, `docs/development.md`가 출발점이다. 파트별 상세 문서와 현재 Issue도 함께 읽는다. 역할표와 허들 댓글이 다르면 작성 시점, 채택 여부, 현재 태스크 배정을 함께 기록한다. 댓글만으로 1급 문서를 바꾸지 않는다.

- 태스크: 실제 변경이 어느 T 번호를 해결하며, 완료 기준 중 무엇이 아직 남았는가? 구현 진행을 작업 완료로 표시하지 않았는가?
- 설계: 상태 전이, 데이터 계약, 승인 경계, 장애 시 동작을 바꾸는가? 변경 필요성과 전후 동작이 문서·코드에서 일치하는가?
- 코드 품질: 책임과 의존성이 명확한가? 같은 정책·상태·클라이언트를 복제했는가? 현재 호출자가 없는 추상화나 별도 오케스트레이터를 만들었는가?
- 보안과 에이전트: 모델은 제안만 하고, 독립 검증기와 Executor가 판정·반영하는가? 도구 allowlist, 읽기 범위, sandbox, 재시도 상한, 실패 시 중지가 실제 실행 경로에서 강제되는가?
- CI: 변경한 컴포넌트와 영향을 받는 소비자가 검사되는가? 선택되지 않은 job의 skip과 실행돼야 할 job의 실패·취소·누락을 구분하는가? 변경마다 무관한 클라우드 절차가 모두 실행되는가?
- 비용과 운영: 영향을 받는 리소스의 component 태그, 예산 알림, IAM 최소 권한, 수명·정리, 상태 저장 위치가 코드와 일치하는가? 비밀 값이 저장소·로그·산출물에 남는가?
- 검증: 로컬 테스트와 해당 변경에 필요한 클라우드 검증이 어느 SHA·환경에서 수행됐는가? 설계, 정적 검사, CI, 실제 클라우드 동작을 구분했는가?

모든 변경에 모든 비용·클라우드 시험을 돌리는 규칙은 만들지 않는다. 해당되지 않는 항목은 이유를 적어 제외한다. 새 네트워크·IAM·예산·클라우드 실행 동작은 로컬 검사만으로 완료 처리하지 않는다. CI 자체를 바꾸면 실패·취소·skip·오래된 head 등 음성 사례도 확인한다.

## 3. 사람이 작성하는 판단 기록

각 패킷 옆에 `assessment.md`를 만든다. 기계가 만든 `UNASSESSED` 목록을 복사한 뒤 원문을 읽고 판단을 채운다. 원본 패킷은 그대로 보관한다. 같은 내용의 스레드가 여러 개면 대표 근거를 연결하되 각 원문이 어떻게 처리됐는지는 남긴다.

```markdown
# PR #73 assessment

- Packet: ./packet.json
- Assessed head: <40-character SHA>
- Assessed base: <40-character SHA>
- Reviewer/agent and date: <identity, UTC time>
- Scope: <T IDs, primary/secondary owners, documents and sections>

| Finding / original comment URL | Disposition | Reason and exact-head evidence | Remaining action / owner |
|---|---|---|---|
| <URL> | fixed | <changed path; tested SHA; command/result; receipt link> | re-read head, then resolve thread |
| <URL> | needs_change | <reproduction and consequence> | <owner/action> |
| <URL> | not_applicable | <authoritative contract or bounded counterexample> | confirm with reviewer if disputed |
| <URL> | needs_owner | <unknown or conflicting contract> | <owner/decision needed> |

## Validation

| Scope | Tested SHA/environment | Evidence | Result and limits |
|---|---|---|---|
| local | <SHA + exact tree state if dirty> | <command/artifact> | <pass/fail/not run> |
| cloud | <SHA + environment> | <provider receipt/run ID> | <observed result, cleanup> |

## Remaining risks and merge state

<Unresolved findings; latest GitHub reviewDecision; exact-head approvals;
required checks; applicable rule constraints. This is not approval.>
```

`fixed`는 수정 사실과 검증 근거가 있다는 뜻이다. `not_applicable`은 단순 취향이나 추측으로 쓰지 않는다. 알 수 없으면 `needs_owner` 또는 `UNASSESSED`로 남긴다. 커밋되지 않은 코드에서 수행한 시험은 그 사실과 tree 차이를 적고 커밋된 SHA 시험으로 표현하지 않는다.

근거에는 재현 명령, 결과 요약, 테스트 파일, 불변 산출물, 클라우드 receipt/run ID를 사용한다. 키·토큰·환경 변수 전체·원시 로그·계정 전체 내역을 복사하지 않는다. 원문 내용을 옮겨야 한다면 필요한 부분만 검토하고 비밀을 제거한다.

## 4. 수정, 스레드 처리, 재리뷰

1. PR의 현재 브랜치와 head를 확인한다. 관련 담당자 변경을 덮어쓰지 않고 `feature/T#-...` 작업에서 지적을 고친다.
2. 영향을 받는 검증을 실행하고 근거를 기록한다. 새 head가 생기면 패킷을 다시 수집한다. 이전 SHA의 승인을 새 SHA 승인으로 세지 않는다.
3. 원문 지적이 실제로 해결됐고 현재 head가 assessment의 SHA와 같으면 고친 스레드를 resolve할 수 있다. 사용자가 승인한 이 작업 범위에서 처리하되 수집기에는 쓰기 동작을 추가하지 않는다. 검증되지 않은 항목을 읽음 표시처럼 resolve하지 않는다.
4. 스레드 resolve와 `CHANGES_REQUESTED` 리뷰는 서로 다르다. 변경 요청 리뷰는 리뷰어가 다시 판단해야 한다. 이를 지우거나 봇·자기 승인을 만들어 통과시키지 않는다. 해결 근거를 갖춰 해당 주·보조 담당자에게 재리뷰를 요청한다.
5. 병합 직전에 head/base, 실제 필수 규칙과 CI, GitHub reviewDecision, 남은 변경 요청을 다시 확인한다. 팀에서 정한 사람 한 명 이상의 승인과 GitHub의 필수 조건을 충족하면 네이티브 auto-merge 절차를 따른다. 초대한 모든 사람의 승인을 별도 장벽으로 만들지 않는다.

이 명령은 요청 전송, 스레드 resolve, 리뷰 dismiss, 승인, 브랜치 변경, auto-merge 설정, 병합을 수행하지 않는다. 그런 행동은 검토 기록을 갖춘 담당자가 현재 사용자 권한과 GitHub 규칙 안에서 수행한다. 수집 결과를 이용한 별도 병합 엔진은 만들지 않는다.
