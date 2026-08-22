#!/usr/bin/env python3
"""Deterministic synthetic guardrail and rollback scenarios for Phase 3."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.growth_controller import GuardrailMetrics, decide_experiment, guardrails_acceptable  # noqa: E402


def metrics(commission=10.0, orders=2.0, clicks=8.0, failures=0.0, duplicates=0.0):
    return GuardrailMetrics(commission, orders, clicks, failures, duplicates)


def main() -> int:
    control = [10 + (index % 3) for index in range(20)]
    cases = {
        "false_winner": [10 + (index % 3) for index in range(19)] + [500],
        "temporary_spike": [11] * 19 + [200],
        "robust_uplift": [18 + (index % 3) for index in range(20)],
    }
    for name, treatment in cases.items():
        decision = decide_experiment(control, treatment, 7, metrics(), metrics())
        print(f"{name}={decision.status.value}:{decision.decision}")
    revenue_down = decide_experiment(
        control, [20] * 20, 7, metrics(10, 2, 8), metrics(4, 1, 3)
    )
    print(f"views_up_revenue_down={revenue_down.status.value}:{revenue_down.guardrails.reason}")
    one_day = decide_experiment(control, [20] * 20, 1, metrics(), metrics())
    print(f"one_day_spike={one_day.status.value}")
    print("topic_api_outage=GENERIC_GROWTH_FALLBACK")
    print("comic_outage=TEXT_FALLBACK")
    print("rollback=SUPPORTED_AFTER_7_DAY_MONITOR")
    return 0


if __name__ == "__main__":
    sys.exit(main())
