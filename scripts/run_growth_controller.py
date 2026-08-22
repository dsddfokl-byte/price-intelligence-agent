#!/usr/bin/env python3
"""Evaluate one bounded growth-controller cycle without publishing Threads posts."""

import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import DATABASE_PATH  # noqa: E402
from app.database import Database  # noqa: E402
from app.growth_controller import (  # noqa: E402
    BoundedGrowthController, bounded_auto_eligibility, current_policy_version,
    ensure_initial_policy,
)
from app.init import initialize_database  # noqa: E402


def main() -> int:
    now = datetime.now(timezone.utc)
    with Database(DATABASE_PATH) as database:
        initialize_database(database)
        ensure_initial_policy(database.connection, now)
        decision = BoundedGrowthController(database.connection).evaluate(now)
        eligibility = bounded_auto_eligibility(database.connection)
        print(f"GROWTH_OPTIMIZER_MODE={decision.mode}")
        print(f"BOUNDED_AUTO_ELIGIBLE={str(eligibility.eligible).lower()}")
        print(f"CURRENT_BOTTLENECK={decision.bottleneck.value}")
        print(f"ACTIVE_EXPERIMENT={decision.active_experiment or 'NONE'}")
        print(f"NEXT_EXPERIMENT={decision.next_experiment or 'NONE'}")
        print(f"CURRENT_POLICY_VERSION={current_policy_version(database.connection)}")
        print(f"MUTATION_APPLIED={str(decision.mutation_applied).lower()}")
        print(f"PERSISTENT_DISTRIBUTION_LOW={str(decision.persistent_low).lower()}")
        print(f"REASON={decision.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
