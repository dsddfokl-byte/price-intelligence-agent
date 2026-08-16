#!/usr/bin/env python3
"""Report optimizer training readiness, statistics, and current ranking."""

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
from app.optimizer_selector import (  # noqa: E402
    MIN_72H_OUTCOMES,
    PerformanceOptimizerSelector,
)
from app.publishers.threads import find_eligible_candidates  # noqa: E402


def format_value(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.2f}"


def main() -> int:
    decided_at = datetime.now(timezone.utc)
    with Database(DATABASE_PATH) as database:
        initialize_database(database)
        state = get_state(database.connection)
        candidates = find_eligible_candidates(database, now=decided_at)
        selector = PerformanceOptimizerSelector(database.connection)
        analysis = selector.analyze(
            candidates, decided_at=decided_at, reward_mode=state.reward_mode
        )

        print("Overview")
        print(f"Reward Mode: {state.reward_mode.value}")
        print(f"Execution State: {state.execution_state.value}")
        print(f"valid 72h outcomes: {analysis.valid_outcomes}")
        print(f"global mean proxy: {format_value(analysis.global_mean_proxy)}")

        print("\nCategory stats")
        if not analysis.category_stats:
            print("データなし")
        for category, stats in sorted(analysis.category_stats.items()):
            readiness = "DATA_SUFFICIENT" if stats.sufficient else "INSUFFICIENT"
            print(
                f"{category}: n={stats.n}, mean={format_value(stats.mean_proxy)}, "
                f"median={format_value(stats.median_proxy)}, "
                f"smoothed={format_value(stats.smoothed_proxy)}, {readiness}"
            )

        print("\nTemplate stats")
        for template in ("PRICE_CONTROL", "OWNER_VALUE"):
            stats = analysis.template_stats.get(template)
            if stats is None:
                print(f"{template}: n=0, INSUFFICIENT")
                continue
            readiness = "DATA_SUFFICIENT" if stats.sufficient else "INSUFFICIENT"
            print(
                f"{template}: n={stats.n}, mean={format_value(stats.mean_proxy)}, "
                f"median={format_value(stats.median_proxy)}, "
                f"smoothed={format_value(stats.smoothed_proxy)}, {readiness}"
            )

        print("\nCurrent Optimizer Candidate Ranking")
        if not analysis.ranking:
            print("eligible candidateなし")
        for item in analysis.ranking[:10]:
            print(
                f"{item.rank_position}. {item.candidate.product.item_code} | "
                f"base={item.base_quality_score:.2f} | "
                f"adjustment={item.historical_adjustment:+.2f} | "
                f"optimizer={item.optimizer_score:.2f}"
            )

        print("\nShadow Decisions")
        decisions = database.connection.execute(
            """
            SELECT * FROM optimizer_shadow_decisions
            ORDER BY decided_at DESC LIMIT 10
            """
        ).fetchall()
        if not decisions:
            print("decisionなし")
        for row in decisions:
            print(
                f"{row['decided_at']} | production={row['production_item_code']} | "
                f"optimizer={row['optimizer_item_code']} | "
                f"selected={row['selected_item_code']} | {row['reason_summary']}"
            )

        print("\nData Readiness")
        print(f"minimum outcomes: {MIN_72H_OUTCOMES}")
        print(f"adjustment enabled: {str(analysis.adjustment_enabled).lower()}")
        if not analysis.adjustment_enabled:
            print("sample不足またはproxy reward以外のため adjustment=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
