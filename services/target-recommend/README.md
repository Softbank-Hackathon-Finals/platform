# T32 이전 대상 추천: 적합성 검사 + AI 추천

설계 8.11 "이전 대상 추천". 입력은 T16 인벤토리 v1의 `candidates`(환경 프로필별 적합성 판정과 제공 기능 대조)이고, 출력은 순위가 붙은 추천 문서다. **권한의 순서**는 다음과 같고 이것이 이슈 #34의 핵심 조건("규칙이 불가면 AI가 뒤집지 못함")이다.

1. 규칙 판정(T16 `fit`): `blocked`는 추천 목록에서 **제외**된다. 모델은 이 후보를 보지도 못한다.
2. 규칙 순위: 남은 후보를 (verdict, 코드 수정 수, 미지 수, 어댑터 수 + 잔존 의존성 수, env_id) 순으로 정렬한다. 결정적이다.
3. AI 순위(선택): 모델이 **정확히 그 목록의 순열**만 돌려주면 받아들인다. 후보를 더하거나 빼거나 `blocked`를 넣거나, 순위가 1..n이 아니거나, 사유가 512자를 넘거나 비밀 패턴을 담으면 거부하고 규칙 순위로 돌아간다. 거부 사유는 열거값(`AI_UNBLOCKED_CANDIDATE` 등)으로 문서에 남는다.

**상태: 로컬 구현 · 시험 13개 통과.** 실제 모델 순위는 T11 도구(`plan.propose`와 같은 경로)로 받을 예정이며 아직 연결하지 않았다. 비용 추정(P2)은 범위 밖.

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
 "excluded": [{"env_id": "onprem-minimal", "verdict": "blocked", "reason": "missing capabilities: s3_compatible, secret_store", "missing_capabilities": ["s3_compatible", "secret_store"]}],
 "ai": {"status": "accepted", "reason": "AI_PERMUTATION_OF_RULE_SET", "model": "…"}}
```

콘솔은 `rule_rank`와 `ai_rank`를 나란히 보여 주고, `excluded`는 "왜 불가인지"와 함께 별도 표시한다. 규칙 근거는 [ADR-0018](../../docs/adr/0018-dependency-grading-rules.md)(규칙이 정하고 에이전트는 빈칸만)과 같다.

## 한계

- 적합성 판정 자체는 T16(`capabilities.fit`)의 것이다. 이 패키지는 판정을 바꾸지 않는다.
- 모델 순위의 "좋음"은 보장하지 않는다. 받아들이는 조건은 형식과 범위뿐이다.
- 비용(이전 중 추가 비용 · 월 운영 비용)은 포함하지 않는다(P2).
