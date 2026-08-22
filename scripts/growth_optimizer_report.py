#!/usr/bin/env python3
"""Read-only Phase 1 Threads growth diagnostics report."""

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import (  # noqa: E402
    DATABASE_PATH,
    GROWTH_MIN_PRACTICAL_UPLIFT,
    GROWTH_MIN_RUNTIME_DAYS,
    GROWTH_MIN_SAMPLES_PER_ARM,
    GROWTH_OPTIMIZER_MODE,
)
from app.database import Database  # noqa: E402
from app.growth_optimizer import GrowthExperimentRegistry, diagnose  # noqa: E402


def value(number: object, digits: int = 2) -> str:
    return "NULL" if number is None else f"{float(number):.{digits}f}"


def main() -> int:
    now = datetime.now(timezone.utc)
    with Database(DATABASE_PATH) as database:
        diagnosis = diagnose(database.connection, now)
        metrics = diagnosis.windows[7]
        try:
            active = GrowthExperimentRegistry(database.connection).active()
        except sqlite3.OperationalError:
            active = None
        replies_per_post = metrics.per_post.get("replies")
        clicks_per_post = metrics.per_post.get("clicks")
        orders_per_post = metrics.per_post.get("confirmed_orders")
        commission = metrics.totals.get("confirmed_commission")

        print(f"GROWTH_OPTIMIZER_MODE={GROWTH_OPTIMIZER_MODE}")
        print(f"CURRENT_BOTTLENECK={diagnosis.current_bottleneck.value}")
        print(f"7D_POSTS={metrics.posts}")
        print(f"MEDIAN_VIEWS={value(metrics.distribution.median)}")
        print(f"P75_VIEWS={value(metrics.distribution.p75)}")
        print(f"REPLIES_PER_POST={value(replies_per_post)}")
        print(f"CLICKS_PER_POST={value(clicks_per_post)}")
        print(f"ORDERS_PER_POST={value(orders_per_post)}")
        print(
            "COMMISSION_PER_DAY="
            + ("NULL" if commission is None else f"{commission / 7:.2f}")
        )
        print(
            "ACTIVE_EXPERIMENT="
            + (str(active["experiment_id"]) if active is not None else "NONE")
        )
        print(
            "SAMPLE_PROGRESS="
            + (
                f"registry_status={active['status']}"
                if active is not None else "NO_PRODUCTION_EXPERIMENT"
            )
        )
        print(
            "DECISION_STATUS="
            + (str(active["decision"] or active["status"]) if active else "SHADOW_ONLY")
        )
        print(f"NEXT_EXPERIMENT_PROPOSAL={diagnosis.next_experiment_proposal}")
        print(f"REASON={diagnosis.reason}")
        print(f"MIN_SAMPLES_PER_ARM={GROWTH_MIN_SAMPLES_PER_ARM}")
        print(f"MIN_RUNTIME_DAYS={GROWTH_MIN_RUNTIME_DAYS}")
        print(f"MIN_PRACTICAL_UPLIFT={GROWTH_MIN_PRACTICAL_UPLIFT:.2f}")

        print("\nWINDOWS")
        for days, window in diagnosis.windows.items():
            distribution = window.distribution
            label = "24h" if days == 1 else f"{days}d"
            print(
                f"{label} posts={window.posts} observed_views={distribution.count} "
                f"mean={value(distribution.mean)} median={value(distribution.median)} "
                f"p25={value(distribution.p25)} p75={value(distribution.p75)} "
                f"p90={value(distribution.p90)}"
            )
        print("\nDIMENSIONS_7D")
        for dimension, groups in metrics.dimensions.items():
            for name, stats in sorted(groups.items()):
                print(
                    f"{dimension}={name} posts={stats.posts} "
                    f"observed_views={stats.observed_views} "
                    f"median_views={value(stats.median_views)}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
