#!/usr/bin/env python3
"""Preview optimizer ranking of the current production-eligible pool."""

import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.autopilot import get_state  # noqa: E402
from app.config import DATABASE_PATH  # noqa: E402
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402
from app.optimizer_selector import PerformanceOptimizerSelector  # noqa: E402
from app.publishers.threads import find_eligible_candidates  # noqa: E402


def main() -> int:
    decided_at = datetime.now(timezone.utc)
    with Database(DATABASE_PATH) as database:
        initialize_database(database)
        state = get_state(database.connection)
        candidates = find_eligible_candidates(database, now=decided_at)
        analysis = PerformanceOptimizerSelector(database.connection).analyze(
            candidates, decided_at=decided_at, reward_mode=state.reward_mode
        )
    print("Optimizer Candidate Preview（Threads投稿なし）")
    print(f"valid 72h outcomes: {analysis.valid_outcomes}")
    print(f"historical adjustment enabled: {str(analysis.adjustment_enabled).lower()}")
    if not analysis.adjustment_enabled:
        print("sample不足のため historical adjustment = 0")
    if not analysis.ranking:
        print("現在、production eligibilityを満たす候補はありません。")
        return 0
    for item in analysis.ranking[:10]:
        print("-")
        print(f"rank: {item.rank_position}")
        print(f"item_code: {item.candidate.product.item_code}")
        print(f"category: {item.candidate.search_keyword}")
        print(f"template_variant: {item.candidate.template_variant}")
        print(f"candidate_source: {item.candidate_source}")
        print(f"base_quality_score: {item.base_quality_score:.2f}")
        print(f"category n: {item.category_n}")
        print(f"category smoothed proxy: {item.category_smoothed_proxy}")
        print(f"template n: {item.template_n}")
        print(f"template smoothed proxy: {item.template_smoothed_proxy}")
        print(f"historical adjustment: {item.historical_adjustment:+.2f}")
        print(f"optimizer_score: {item.optimizer_score:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
