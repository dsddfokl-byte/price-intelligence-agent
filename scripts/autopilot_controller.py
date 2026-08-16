#!/usr/bin/env python3
"""Run one idempotent autopilot health/state evaluation without publishing."""

import argparse
import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.autopilot import (  # noqa: E402
    AutopilotController,
    ExecutionState,
    HaltClass,
    RewardMode,
    emergency_safe_halt,
    get_state,
    new_controller_evaluation_id,
    quick_integrity_check,
)
from app.config import DATABASE_PATH  # noqa: E402
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate autopilot safety state")
    parser.add_argument(
        "--evaluation-id",
        default=None,
        help="Reuse this id on retry to make evaluation idempotent",
    )
    return parser.parse_args()


def enabled(name: str) -> bool:
    return os.getenv(name, "").lower() in ("1", "true", "yes")


def main() -> int:
    args = parse_args()
    evaluation_id = args.evaluation_id or new_controller_evaluation_id()
    with Database(DATABASE_PATH) as database:
        initialize_database(database)
        if not quick_integrity_check(database.connection):
            AutopilotController(database.connection).evaluate(
                evaluation_id,
                global_safety_error="DB integrity failure",
                halt_class=HaltClass.MANUAL_REVIEW_REQUIRED,
            )
            print("Execution State: SAFE_HALT")
            return 1
        try:
            state = get_state(database.connection)
        except Exception:
            state = emergency_safe_halt(
                database.connection, evaluation_id, "State machine corruption"
            )
            print(f"Execution State: {state.execution_state.value}")
            return 1

        controller = AutopilotController(database.connection)
        if state.execution_state == ExecutionState.SAFE_HALT:
            state = controller.evaluate(
                evaluation_id,
                health_check_passed=True,
                clear_safe_halt=enabled("AUTOPILOT_CLEAR_SAFE_HALT"),
            )
        elif state.execution_state == ExecutionState.FALLBACK_CONTROL:
            state = controller.evaluate(
                evaluation_id, requested_state=ExecutionState.SHADOW
            )
        elif state.execution_state == ExecutionState.ADAPTIVE_LIVE:
            # No post-level Revenue Provider exists yet; never retain Adaptive
            # Live on proxy data.
            state = controller.evaluate(
                evaluation_id, observed_reward_mode=RewardMode.ENGAGEMENT_PROXY
            )
        else:
            state = controller.evaluate(evaluation_id)
        print(f"Controller Evaluation ID: {evaluation_id}")
        print(f"Execution State: {state.execution_state.value}")
        print(f"Reward Mode: {state.reward_mode.value}")
        print(f"Experiment Epoch: {state.experiment_epoch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
