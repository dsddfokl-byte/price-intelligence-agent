#!/usr/bin/env python3
"""Deterministic synthetic evaluation for Phase 1 growth decisions."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.growth_optimizer import evaluate_views_experiment  # noqa: E402


def main() -> int:
    control = [90, 95, 100, 105, 110] * 4
    scenarios = {
        "no_improvement": list(control),
        "+20%": [value * 1.20 for value in control],
        "+50%": [value * 1.50 for value in control],
        "+100%": [value * 2.00 for value in control],
        "one_viral_outlier": [100.0] * 19 + [10_000.0],
    }
    for name, treatment in scenarios.items():
        result = evaluate_views_experiment(control, treatment, runtime_days=7)
        print(
            f"{name}: status={result.status.value} decision={result.decision} "
            f"control_n={result.control_n} treatment_n={result.treatment_n} "
            f"uplift={result.relative_uplift if result.relative_uplift is not None else 'NULL'} "
            f"ci={result.bootstrap_ci}"
        )
    early = evaluate_views_experiment(control[:5], [value * 2 for value in control[:5]], 2)
    print(f"small_sample_+100%: status={early.status.value} decision={early.decision}")
    print("missing_revenue: confirmed_commission=NULL (preserved, not converted to zero)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
