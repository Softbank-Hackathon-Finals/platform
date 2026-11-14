# T32 이전 대상 추천: 적합성 검사 + AI 추천

설계 8.11 "이전 대상 추천". 입력은 T16 인벤토리 v1의 `candidates`(환경 프로필별 적합성 판정과 제공 기능 대조)이고, 출력은 순위가 붙은 추천 문서다. **권한의 순서**는 다음과 같고 이것이 이슈 #34의 핵심 조건("규칙이 불가면 AI가 뒤집지 못함")이다.

1. 규칙 판정(T16 `fit`): `blocked`는 추천 목록에서 **제외**된다. 모델은 이 후보를 보지도 못한다.
2. 규칙 순위: 남은 후보를 (verdict, 코드 수정 수, 미지 수, 어댑터 수 + 잔존 의존성 수, env_id) 순으로 정렬한다. 결정적이다.
3. AI 순위(선택): 모델이 **정확히 그 목록의 순열**만 돌려주면 받아들인다. 후보를 더하거나 빼거나 `blocked`를 넣거나, 순위가 1..n이 아니거나, 사유가 512자를 넘거나 비밀 패턴을 담으면 거부하고 규칙 순위로 돌아간다. 거부 사유는 열거값(`AI_UNBLOCKED_CANDIDATE` 등)으로 문서에 남는다.

**상태: 로컬 구현 · 시험 26개 통과.** 실제 모델 순위는 T11 도구(`plan.propose`와 같은 경로)로 받을 예정이며 아직 연결하지 않았다. 비용 추정(P2)은 범위 밖.

| 파일 | 역할 |
|---|---|
| `recommend.py` | `rule_rank` · `validate_ai_ranking` · `recommend`(스키마 검증) · `FakeRanker` |
| `schemas/recommendation.schema.json` | 추천 v1. `candidates[].verdict`는 `fits` · `needs_work`만 허용(`blocked`는 `excluded[]`에만) |
| `cli.py` | `bravo-recommend --inventory T16.json --output rec.json [--ai-ranking model.json]` |

## 출력 예

```json
{"schema_version": "1.0", "app_id": "demo-chat", "inventory_digest": "sha256:…", "ranked_by": "rule+ai",
 "recommendation": "onprem-demo",
 "candidates": [{"env_id": "onprem-demo", "env_kind": "onprem", "verdict": "needs_work", "rule_rank": 2, "ai_rank": 1,
                 "score": [1, 2, 0, 3], "grade_counts": {"config_swap": 2, "adapter": 1, "code_change": 2},
                 "missing_capabilities": [], "residual_dependencies": ["cloudflare (edge · tunnel)", "ghcr.io (image pull)"],
                 "rule_reasons": ["verdict needs_work", "2 code change(s)", "1 adapter(s)", "2 residual dependency(ies)"],
                 "ai_reason": "operations team already runs RKE2"}],
 "excluded": [{"env_id": "onprem-minimal", "verdict": "blocked", "reason_codes": ["MISSING_CAPABILITY"],
               "reason": "missing capabilities: s3_compatible, secret_store",
               "missing_capability_keys": ["s3_compatible", "secret_store"], "no_replacement_count": 0,
               "missing_capabilities": ["object_store 대체(seaweedfs)를 뒷받침할 's3_compatible' 기능이 등록되지 않음", "…"]}],
 "ai": {"status": "accepted", "reason": "AI_PERMUTATION_OF_RULE_SET", "model": "…"}}
```

콘솔은 `rule_rank`와 `ai_rank`를 나란히 보여 주고, `excluded`는 "왜 불가인지"와 함께 별도 표시한다.

### `excluded[]`의 불가 사유

T16은 `missing_capabilities`를 한국어 문장으로 쓴다. 이 문장을 이어 붙이면 기능 3개 부족과 `none` 항목만으로 `reason`의 200자 상한을 넘어 `recommend()`가 스키마 검증에서 실패했다. 사유는 구조화해 둔다.

| 필드 | 내용 |
|---|---|
| `reason_codes` | `MISSING_CAPABILITY` · `NO_REPLACEMENT` · `BLOCKED_BY_RULE`(T16이 `blocked`지만 두 근거가 모두 없을 때) |
| `missing_capability_keys` | T16 문장에서 따옴표로 지정한 기능 키(`s3_compatible` 등), 정렬 · 중복 제거. 키 하나를 특정할 수 없는 문장은 넣지 않고 `reason`에 개수로만 센다 |
| `no_replacement_count` | 대체 없음(`none`) 등급 항목 수 |
| `reason` | 위 값으로 다시 만든 사람용 요약(200자 이하). 키 목록이 길면 개수 표시로 바꾸며 문자열을 중간에서 자르지 않는다 |
| `missing_capabilities` | T16 원문 그대로(콘솔 상세 표시용) |

11/6 추가: `reason`·`missing_capabilities`의 의미는 그대로이고 앞의 세 필드를 더했다. 저장소 안에 이 문서를 읽는 소비자가 아직 없어 `schema_version`은 `1.0`으로 둔다. 출력 스키마는 새 필드를 필수로 요구하므로 이 스키마로 검증하는 소비자는 이 버전 이후 출력만 받는다.

회귀 시험은 손으로 만든 dict가 아니라 T16 `bravo-inventory scan`을 그 fixture(`demo-chat` + `exports/aws` + `exports/onprem`)에 실제로 실행한 출력을 쓴다. 시험 전용 의존성으로 `../inventory`를 path dev 의존성으로 둔다(런타임 의존성 아님). 규칙 근거는 [ADR-0018](../../docs/adr/0018-dependency-grading-rules.md)(규칙이 정하고 에이전트는 빈칸만)과 같다.

### 출발지 · 목적지 종류 (ADR-0034, A4)

출발지와 후보는 aws · gcp · onprem 어느 것이든 된다. 후보 점수는 종류와 상관없이 같은 규칙(T16 조합 카탈로그로 매긴 등급 수)으로 매긴다. 새 필드는 모두 선택 필드라 옛 인벤토리 · 옛 출력도 그대로 검증된다.

| 필드 | 내용 |
|---|---|
| `source_kind` | 인벤토리 `source.env_kind`. 출발지를 AWS로 가정하지 않는다. 옛 인벤토리에 없으면 생략 |
| `source_services` | 인벤토리 항목의 서비스 이름(종류 중립: aws면 `s3`, gcp면 `storage` 등) |
| `candidates[].migration` | `same_kind_cluster`(같은 종류: 다른 클러스터로의 이전) · `cross_kind`. 모델에게도 보인다 |

## 한계

- 적합성 판정 자체는 T16(`capabilities.fit`)의 것이다. 이 패키지는 판정을 바꾸지 않는다.
- 모델 순위의 "좋음"은 보장하지 않는다. 받아들이는 조건은 형식과 범위뿐이다.
- 비용(이전 중 추가 비용 · 월 운영 비용)은 포함하지 않는다(P2).
