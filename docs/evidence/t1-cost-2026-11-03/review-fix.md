# PR #74 비용 귀속 판정 수정

[이상무님의 리뷰](https://github.com/Softbank-Hackathon-Finals/platform/pull/74#discussion_r4451424020)를 반영했다. 두 태그가 Active일 때 미귀속 비용 +10 USD와 환불 -10 USD가 상쇄되면 기존 코드는 귀속 완료로 표시했다. 팀 프로젝트의 미등록 component 비용에도 같은 문제가 있었다.

순금액 계산은 유지하고 `project_unattributed.group_count`, `bravo_unknown_component.group_count`를 추가했다. 귀속 완료는 두 태그가 Active이고 두 그룹 수가 모두 0일 때만 표시한다. 금액이 0인 그룹도 센다. 이 수는 Cost Explorer가 반환한 일자·project·component 조합의 수이며 청구 항목 수는 아니다.

## 로컬 검증

- 수정 전 새 회귀 시험에서 미귀속 비용·환불 상쇄, 미등록 component 상쇄, 0원 미귀속 세 경우의 잘못된 true 판정을 재현했다.
- `python3 -m unittest discover -s scripts/tests -v`: **19개 통과**. 새 시험 4개에는 0원 미귀속의 하위 사례 3개와, 정상 귀속된 비용·환불의 순합계 0이 true를 유지하는 대조군이 포함된다.
- `uvx ruff check --target-version py310 scripts/cost_report.py scripts/tests/test_cost_report.py`: 통과. 문서의 Python 3.10 이상 지원을 기준으로 검사했다.
- `git diff --check`: 통과.

## 실제 AWS 읽기 검증

2026-11-03 15:12 KST에 수정된 도구로 11/1 포함~11/3 제외 비용을 다시 조회했다. 계정 확인 후 비용 배분 태그와 Cost Explorer만 읽었다. AWS 자원·권한·경보 설정은 변경하지 않았다.

| 항목 | 결과 |
|---|---|
| `project` 태그 상태 | NOT_AVAILABLE |
| `component` 태그 상태 | Active |
| 미귀속 일별 그룹 | 2 |
| 팀 프로젝트의 미등록 component 그룹 | 0 |
| `attribution_complete` | false |
| AWS 추정 비용 여부 | true |

[재조회 요약](review-cost-readback.json)에 시각·범위·태그 상태·그룹 수를 기록했다. 원시 비용 결과는 개인 경로에 보관했다. 이번 실계정 조회는 입력 처리·API 연결·현재 귀속 상태 확인이며, 비용과 환불 상쇄 결함의 재현은 로컬 회귀 시험의 근거다. `project` 키 활성화와 실제 예산 초과 경보 배달은 기존 후속 검증으로 남는다.
