"""Safety, statistics, and atomicity tests for the autopilot controller."""

import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.autopilot import (
    DEEP_ENGAGEMENT_MIN_VIEWS,
    PRIMARY_CLICK_METRIC,
    PRIMARY_REVENUE_METRIC,
    SECONDARY_CLICK_METRIC,
    SECONDARY_REVENUE_METRICS,
    AutopilotController,
    ExecutionState,
    ExperimentArm,
    HaltClass,
    OutcomeStatus,
    RewardComparison,
    RewardMode,
    StateValidationError,
    causal_rewards,
    compare_rewards,
    deep_engagement_reward,
    emergency_safe_halt,
    get_state,
    outcome_status,
    runtime_policy,
    stable_arm_assignment,
    validate_state_combination,
)
from app.database import Database


NOW = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)


class AutopilotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "autopilot.db"
        self.database = Database(self.path)
        self.database.initialize()

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def _set_state(
        self,
        execution: ExecutionState,
        reward: RewardMode,
        *,
        entered_at: datetime = NOW,
    ) -> None:
        with self.database.connection:
            self.database.connection.execute(
                """
                UPDATE autopilot_state
                SET execution_state=?, reward_mode=?, state_entered_at=?, updated_at=?
                WHERE id=1
                """,
                (execution.value, reward.value, entered_at.isoformat(), entered_at.isoformat()),
            )

    def _record_outcome(
        self,
        *,
        epoch: str,
        arm: ExperimentArm,
        key: str,
        formal: bool,
        status: OutcomeStatus,
        commission: float = 0,
        clicks: int = 0,
        views: int = 100,
    ) -> None:
        with self.database.connection:
            self.database.connection.execute(
                "INSERT OR IGNORE INTO products(item_code,first_seen_at,last_seen_at) VALUES (?,?,?)",
                (key, NOW.isoformat(), NOW.isoformat()),
            )
            self.database.connection.execute(
                """
                INSERT INTO experiment_cycles(
                    cycle_id,experiment_epoch,experiment_arm,assignment_key,
                    reward_mode,assigned_at,is_formal_experiment
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    f"cycle-{key}", epoch, arm.value, key, RewardMode.REVENUE.value,
                    NOW.isoformat(), int(formal),
                ),
            )
            self.database.connection.execute(
                """
                INSERT INTO threads_posts(
                    item_code,posted_at,deal_score,text_hash,status,
                    experiment_arm,assignment_key,experiment_epoch,outcome_status,
                    confirmed_commission,clicks,views,replies,reposts,quotes,shares
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    key, NOW.isoformat(), 80, key, "published", arm.value, key,
                    epoch, status.value, commission, clicks, views, 1, 0, 0, 0,
                ),
            )

    def test_allowed_state_reward_combinations(self) -> None:
        with self.assertRaises(StateValidationError):
            validate_state_combination(
                ExecutionState.ADAPTIVE_LIVE, RewardMode.ENGAGEMENT_PROXY
            )
        with self.assertRaises(StateValidationError):
            validate_state_combination(
                ExecutionState.ADAPTIVE_LIVE, RewardMode.CLICK_PROXY
            )
        validate_state_combination(ExecutionState.ADAPTIVE_LIVE, RewardMode.REVENUE)

    def test_revenue_downgrade_is_atomic_and_changes_epoch(self) -> None:
        self._set_state(ExecutionState.ADAPTIVE_LIVE, RewardMode.REVENUE)
        old = get_state(self.database.connection)
        result = AutopilotController(self.database.connection).evaluate(
            "downgrade", now=NOW, observed_reward_mode=RewardMode.CLICK_PROXY
        )
        self.assertEqual(result.execution_state, ExecutionState.LIMITED_LIVE)
        self.assertEqual(result.reward_mode, RewardMode.CLICK_PROXY)
        self.assertNotEqual(result.experiment_epoch, old.experiment_epoch)
        transition = self.database.connection.execute(
            "SELECT * FROM autopilot_transitions WHERE controller_evaluation_id='downgrade'"
        ).fetchone()
        self.assertEqual(transition["to_execution_state"], "LIMITED_LIVE")
        self.assertEqual(transition["to_reward_mode"], "CLICK_PROXY")

    def test_global_and_optimizer_failures_have_distinct_states(self) -> None:
        controller = AutopilotController(self.database.connection)
        state = controller.evaluate(
            "db-failure", now=NOW, global_safety_error="DB integrity failure"
        )
        self.assertEqual(state.execution_state, ExecutionState.SAFE_HALT)

        self.database.close()
        self.database = Database(Path(self.temp.name) / "optimizer.db")
        self.database.initialize()
        state = AutopilotController(self.database.connection).evaluate(
            "optimizer-failure", now=NOW, optimizer_error="scoring exception"
        )
        self.assertEqual(state.execution_state, ExecutionState.FALLBACK_CONTROL)

    def test_persistent_safe_halt_requires_manual_clear(self) -> None:
        controller = AutopilotController(self.database.connection)
        controller.evaluate(
            "halt", now=NOW, global_safety_error="security issue",
            halt_class=HaltClass.MANUAL_REVIEW_REQUIRED,
        )
        still_halted = controller.evaluate(
            "health", now=NOW + timedelta(days=2), health_check_passed=True
        )
        self.assertEqual(still_halted.execution_state, ExecutionState.SAFE_HALT)
        cleared = controller.evaluate(
            "manual-clear", now=NOW + timedelta(days=2), clear_safe_halt=True
        )
        self.assertEqual(cleared.execution_state, ExecutionState.SHADOW)

    def test_transient_safe_halt_needs_two_checks_and_24_hours(self) -> None:
        controller = AutopilotController(self.database.connection)
        controller.evaluate(
            "halt", now=NOW, global_safety_error="temporary schema lock",
            halt_class=HaltClass.TRANSIENT,
        )
        first = controller.evaluate("health-1", now=NOW, health_check_passed=True)
        self.assertEqual(first.execution_state, ExecutionState.SAFE_HALT)
        recovered = controller.evaluate(
            "health-2", now=NOW + timedelta(hours=24), health_check_passed=True
        )
        self.assertEqual(recovered.execution_state, ExecutionState.SHADOW)

    def test_reward_and_version_changes_start_new_epochs(self) -> None:
        controller = AutopilotController(self.database.connection)
        original = get_state(self.database.connection)
        reward_changed = controller.evaluate(
            "reward", now=NOW, observed_reward_mode=RewardMode.CLICK_PROXY
        )
        self.assertNotEqual(reward_changed.experiment_epoch, original.experiment_epoch)
        model_changed = controller.evaluate(
            "model", now=NOW, optimizer_model_version="optimizer-v2"
        )
        self.assertNotEqual(model_changed.experiment_epoch, reward_changed.experiment_epoch)

    def test_old_epoch_immature_and_observational_data_are_excluded(self) -> None:
        epoch = get_state(self.database.connection).experiment_epoch
        self._record_outcome(
            epoch="old-epoch", arm=ExperimentArm.CONTROL, key="old", formal=True,
            status=OutcomeStatus.MATURE, commission=10,
        )
        self._record_outcome(
            epoch=epoch, arm=ExperimentArm.CONTROL, key="immature", formal=True,
            status=OutcomeStatus.IMMATURE, commission=99,
        )
        self._record_outcome(
            epoch=epoch, arm=ExperimentArm.OPTIMIZER, key="prior", formal=False,
            status=OutcomeStatus.MATURE, commission=99,
        )
        self._record_outcome(
            epoch=epoch, arm=ExperimentArm.OPTIMIZER, key="formal", formal=True,
            status=OutcomeStatus.MATURE, commission=12,
        )
        control, optimizer, immature = causal_rewards(
            self.database.connection, epoch, RewardMode.REVENUE
        )
        self.assertEqual(control, [])
        self.assertEqual(optimizer, [12.0])
        self.assertEqual(immature, 1)

    def test_maturity_never_turns_immature_into_zero(self) -> None:
        self.assertEqual(
            outcome_status(NOW, NOW + timedelta(hours=1), timedelta(days=2)),
            OutcomeStatus.IMMATURE,
        )
        self.assertEqual(outcome_status(NOW, NOW, None), OutcomeStatus.UNKNOWN)

    def test_sha256_assignment_is_stable(self) -> None:
        first = stable_arm_assignment(
            "epoch", "cycle", ExecutionState.LIMITED_LIVE
        )
        second = stable_arm_assignment(
            "epoch", "cycle", ExecutionState.LIMITED_LIVE
        )
        self.assertEqual(first, second)
        assignments = {
            stable_arm_assignment("epoch", f"cycle-{i}", ExecutionState.LIMITED_LIVE)[0]
            for i in range(100)
        }
        self.assertEqual(assignments, {ExperimentArm.CONTROL, ExperimentArm.OPTIMIZER})

    def test_duplicate_evaluation_id_is_idempotent(self) -> None:
        controller = AutopilotController(self.database.connection)
        controller.evaluate("same", now=NOW, requested_state=ExecutionState.LIMITED_LIVE)
        controller.evaluate("same", now=NOW, requested_state=ExecutionState.SHADOW)
        count = self.database.connection.execute(
            "SELECT COUNT(*) FROM controller_evaluations WHERE controller_evaluation_id='same'"
        ).fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(get_state(self.database.connection).execution_state, ExecutionState.LIMITED_LIVE)

    def test_concurrent_evaluation_is_single_transition(self) -> None:
        self.database.close()
        barrier = threading.Barrier(2)
        errors = []

        def worker() -> None:
            db = Database(self.path)
            db.initialize()
            try:
                barrier.wait()
                AutopilotController(db.connection).evaluate(
                    "concurrent", now=NOW, requested_state=ExecutionState.LIMITED_LIVE
                )
            except Exception as error:  # pragma: no cover - diagnostic capture
                errors.append(error)
            finally:
                db.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.database = Database(self.path)
        self.assertEqual(errors, [])
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM controller_evaluations WHERE controller_evaluation_id='concurrent'"
            ).fetchone()[0],
            1,
        )

    def test_api_outage_does_not_change_state(self) -> None:
        result = AutopilotController(self.database.connection).evaluate(
            "outage", now=NOW, requested_state=ExecutionState.LIMITED_LIVE,
            api_outage=True,
        )
        self.assertEqual(result.execution_state, ExecutionState.SHADOW)

    def test_low_view_gate_and_revenue_outlier_robustness(self) -> None:
        self.assertEqual(DEEP_ENGAGEMENT_MIN_VIEWS, 50)
        self.assertIsNone(deep_engagement_reward(5, 1, 0, 0, 0))
        self.assertEqual(deep_engagement_reward(100, 1, 0, 0, 0), 0.01)
        comparison = compare_rewards([10.0] * 20, [10.0] * 19 + [10000.0])
        self.assertFalse(comparison.robustly_better)

    def test_primary_and_secondary_objectives(self) -> None:
        self.assertEqual(PRIMARY_REVENUE_METRIC, "commission_per_published_post")
        self.assertIn("commission_per_1000_views", SECONDARY_REVENUE_METRICS)
        self.assertEqual(PRIMARY_CLICK_METRIC, "clicks_per_published_post")
        self.assertEqual(SECONDARY_CLICK_METRIC, "clicks_per_1000_views")

    def test_safe_halt_forbids_publish(self) -> None:
        controller = AutopilotController(self.database.connection)
        state = controller.evaluate(
            "halt", now=NOW, global_safety_error="disclosure failure"
        )
        self.assertTrue(runtime_policy(state, {}).halt_publish)

    def test_corrupt_state_is_repaired_to_safe_halt(self) -> None:
        with self.database.connection:
            self.database.connection.execute(
                "UPDATE autopilot_state SET execution_state='BROKEN' WHERE id=1"
            )
        state = emergency_safe_halt(
            self.database.connection, "emergency", "State machine corruption", now=NOW
        )
        self.assertEqual(state.execution_state, ExecutionState.SAFE_HALT)
        self.assertTrue(state.manual_clear_required)

    def test_publish_failures_are_counted_by_arm(self) -> None:
        with self.database.connection:
            for index, (arm, status) in enumerate(
                (("CONTROL", "published"), ("OPTIMIZER", "failed"))
            ):
                self.database.connection.execute(
                    """
                    INSERT INTO experiment_cycles(
                        cycle_id,experiment_epoch,experiment_arm,assignment_key,
                        reward_mode,assigned_at,publish_attempted,publish_status
                    ) VALUES (?,?,?,?,?,?,1,?)
                    """,
                    (
                        f"report-{index}", "epoch", arm, f"key-{index}",
                        "ENGAGEMENT_PROXY", NOW.isoformat(), status,
                    ),
                )
        rows = self.database.connection.execute(
            """
            SELECT experiment_arm, SUM(publish_attempted) attempts,
                   SUM(CASE WHEN publish_status='failed' THEN 1 ELSE 0 END) failures
            FROM experiment_cycles GROUP BY experiment_arm
            """
        ).fetchall()
        counts = {row["experiment_arm"]: dict(row) for row in rows}
        self.assertEqual(counts["CONTROL"]["attempts"], 1)
        self.assertEqual(counts["OPTIMIZER"]["failures"], 1)

    def test_revenue_promotion_needs_two_robust_evaluations(self) -> None:
        self._set_state(
            ExecutionState.LIMITED_LIVE,
            RewardMode.REVENUE,
            entered_at=NOW - timedelta(days=2),
        )
        evidence = RewardComparison(
            20, 20, 10, 11, 10, 11, 10, 11, 0.95, 0.10, True
        )
        controller = AutopilotController(self.database.connection)
        first = controller.evaluate(
            "promotion-1", now=NOW, requested_state=ExecutionState.ADAPTIVE_LIVE,
            evidence=evidence,
        )
        self.assertEqual(first.execution_state, ExecutionState.LIMITED_LIVE)
        second = controller.evaluate(
            "promotion-2", now=NOW + timedelta(days=1),
            requested_state=ExecutionState.ADAPTIVE_LIVE, evidence=evidence,
        )
        self.assertEqual(second.execution_state, ExecutionState.ADAPTIVE_LIVE)


if __name__ == "__main__":
    unittest.main()
