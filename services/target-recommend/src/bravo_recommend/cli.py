"""bravo-recommend: T16 inventory → ranked migration targets (rules first, model may only reorder)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .recommend import FakeRanker, recommend


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="bravo-recommend")
    parser.add_argument("--inventory", required=True, help="T16 inventory document (v1)")
    parser.add_argument("--output", required=True)
    parser.add_argument("--ai-ranking", default="", help="optional model ranking JSON to validate and merge (e.g. T11 tool output)")
    args = parser.parse_args(argv)
    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    ranker = FakeRanker(json.loads(Path(args.ai_ranking).read_text(encoding="utf-8"))) if args.ai_ranking else None
    doc = recommend(inventory, ranker)
    out = Path(args.output)
    if out.exists():
        parser.error("output exists; refusing to overwrite")
    out.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"recommendation": doc["recommendation"], "ranked_by": doc["ranked_by"], "ai": doc["ai"],
                      "candidates": [c["env_id"] for c in doc["candidates"]], "excluded": [e["env_id"] for e in doc["excluded"]]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
