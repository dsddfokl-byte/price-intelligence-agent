#!/usr/bin/env python3
"""Read-only historical Threads growth backtest."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import DATABASE_PATH  # noqa: E402
from app.database import Database  # noqa: E402
from app.growth_optimizer import arm_views, diagnose, evaluate_views_experiment  # noqa: E402


def evaluate_pair(label: str, arms: dict, control: str, treatment: str) -> None:
    result = evaluate_views_experiment(
        arms.get(control, ()), arms.get(treatment, ()), runtime_days=28
    )
    print(
        f"{label}: control={control} n={result.control_n}, "
        f"treatment={treatment} n={result.treatment_n}, "
        f"hypothetical_decision={result.decision}, status={result.status.value}, "
        f"reason={result.reason}"
    )


def main() -> int:
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=28)
    with Database(DATABASE_PATH) as database:
        diagnosis = diagnose(database.connection, now)
        print(f"historical_bottleneck={diagnosis.current_bottleneck.value}")
        print(f"reason={diagnosis.reason}")
        templates = arm_views(
            database.connection, "template_variant", since, now
        )
        media = arm_views(
            database.connection, "delivered_media_variant", since, now
        )
        evaluate_pair(
            "template_observational_only", templates,
            "PRICE_CONTROL", "OWNER_VALUE",
        )
        evaluate_pair(
            "media_observational_only", media, "NO_COMIC", "COMIC"
        )
        print("sample_limitations=Historical groups are observational, not causal assignments")
        print("production_mutations=0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
