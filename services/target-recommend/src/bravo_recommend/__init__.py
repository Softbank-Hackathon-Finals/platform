"""T32: recommend a migration target. Rules decide what is possible; a model may only order it."""

from .recommend import AIRankingRejected, FakeRanker, recommend, rule_rank, validate_ai_ranking

__all__ = ["AIRankingRejected", "FakeRanker", "recommend", "rule_rank", "validate_ai_ranking"]
