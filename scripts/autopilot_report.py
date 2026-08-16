#!/usr/bin/env python3
"""Report safety state, experiment exposure, and reward readiness."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.autopilot import (  # noqa: E402
    PRIMARY_CLICK_METRIC,
    PRIMARY_REVENUE_METRIC,
    SECONDARY_CLICK_METRIC,
    SECONDARY_REVENUE_METRICS,
    ExperimentArm,
    OutcomeStatus,
    RewardMode,
    get_state,
    maximum_execution_state,
)
from app.config import DATABASE_PATH  # noqa: E402
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402


def main() -> int:
    with Database(DATABASE_PATH) as database:
        initialize_database(database)
        state = get_state(database.connection)
        print(f"Execution State: {state.execution_state.value}")
        print(f"SAFE_HALT status: {'ACTIVE' if state.execution_state.value == 'SAFE_HALT' else 'INACTIVE'}")
        if state.safe_halt_reason:
            print(f"SAFE_HALT class: {state.safe_halt_class.value}")
            print(f"SAFE_HALT reason: {state.safe_halt_reason}")
        print(f"Reward Mode: {state.reward_mode.value}")
        print(
            "Maximum Allowed Execution State: "
            f"{maximum_execution_state(state.reward_mode).value}"
        )
        print(f"Revenue Data Ready: {str(state.revenue_data_ready).lower()}")
        print(f"Experiment Epoch: {state.experiment_epoch}")

        if state.reward_mode == RewardMode.REVENUE:
            primary = PRIMARY_REVENUE_METRIC
            secondary = ", ".join(SECONDARY_REVENUE_METRICS)
        elif state.reward_mode == RewardMode.CLICK_PROXY:
            primary = PRIMARY_CLICK_METRIC
            secondary = SECONDARY_CLICK_METRIC
        else:
            primary = "deep_engagement_per_published_post (views >= 50)"
            secondary = "views, replies, reposts, quotes, shares"
        print(f"Primary Objective: {primary}")
        print(f"Secondary Efficiency Metric: {secondary}")

        outcome_counts = database.connection.execute(
            """
            SELECT
                SUM(CASE WHEN outcome_status = 'IMMATURE' THEN 1 ELSE 0 END) immature,
                SUM(CASE WHEN outcome_status = 'UNKNOWN' OR outcome_status IS NULL THEN 1 ELSE 0 END) unknown
            FROM threads_posts
            WHERE experiment_epoch = ?
            """,
            (state.experiment_epoch,),
        ).fetchone()
        print("Reward Maturity:")
        print(f"  immature outcomes: {int(outcome_counts['immature'] or 0)}")
        print(f"  unknown outcomes: {int(outcome_counts['unknown'] or 0)}")
        revenue = database.connection.execute(
            """
            SELECT COALESCE(SUM(pending_orders), 0) pending_orders,
                   COALESCE(SUM(pending_commission), 0) pending_commission,
                   COALESCE(SUM(confirmed_orders), 0) confirmed_orders,
                   COALESCE(SUM(confirmed_commission), 0) confirmed_commission
            FROM threads_posts WHERE experiment_epoch = ?
            """,
            (state.experiment_epoch,),
        ).fetchone()
        print(f"  pending orders: {int(revenue['pending_orders'])}")
        print(f"  pending commission: {float(revenue['pending_commission']):.2f}")
        print(f"  confirmed orders: {int(revenue['confirmed_orders'])}")
        print(f"  confirmed commission: {float(revenue['confirmed_commission']):.2f}")

        for arm in ExperimentArm:
            counts = database.connection.execute(
                """
                SELECT COUNT(*) assigned,
                       SUM(CASE WHEN selected_item_code IS NOT NULL THEN 1 ELSE 0 END) selected,
                       SUM(publish_attempted) attempts,
                       SUM(CASE WHEN publish_status = 'published' THEN 1 ELSE 0 END) published,
                       SUM(CASE WHEN publish_status = 'failed' THEN 1 ELSE 0 END) failures
                FROM experiment_cycles
                WHERE experiment_epoch = ? AND experiment_arm = ?
                """,
                (state.experiment_epoch, arm.value),
            ).fetchone()
            mature = database.connection.execute(
                """
                SELECT COUNT(*) count FROM threads_posts
                WHERE experiment_epoch = ? AND experiment_arm = ?
                  AND outcome_status = ? AND status = 'published'
                """,
                (state.experiment_epoch, arm.value, OutcomeStatus.MATURE.value),
            ).fetchone()
            print(f"{arm.value}:")
            print(f"  assigned cycles: {int(counts['assigned'] or 0)}")
            print(f"  selected posts: {int(counts['selected'] or 0)}")
            print(f"  publish attempts: {int(counts['attempts'] or 0)}")
            print(f"  published: {int(counts['published'] or 0)}")
            print(f"  publish failures: {int(counts['failures'] or 0)}")
            attempts = int(counts["attempts"] or 0)
            published = int(counts["published"] or 0)
            rate = published / attempts * 100 if attempts else 0
            print(f"  publish success rate: {rate:.2f}%")
            print(f"  mature outcomes: {int(mature['count'] or 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
