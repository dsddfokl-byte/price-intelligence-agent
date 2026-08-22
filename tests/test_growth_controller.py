import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import AUTO_REPLY_MODE, GROWTH_OPTIMIZER_MODE, THREADS_PUBLISHING
from app.database import Database
from app.growth_controller import (
    BoundedGrowthController, ControllerMode, EXPERIMENT_QUEUES,
    GuardrailMetrics, apply_policy_mutation, bounded_auto_eligibility,
    current_policy_version, decide_experiment, ensure_initial_policy,
    guardrails_acceptable, persistent_distribution_low, rollback_policy,
    select_next_experiment, validate_mutation,
    start_next_experiment,
)
from app.growth_optimizer import Bottleneck, ExperimentStatus, MUTATION_ALLOWLIST


def guardrails(commission=10.0, orders=2.0, clicks=8.0, failures=0.0, duplicates=0.0):
    return GuardrailMetrics(commission, orders, clicks, failures, duplicates)


class GrowthControllerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "test.db")
        self.database.initialize()
        self.now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        ensure_initial_policy(self.database.connection, self.now - timedelta(days=20))

    def tearDown(self):
        self.database.close()
        self.temporary.cleanup()

    def test_adopt_reject_and_inconclusive(self):
        control = [10 + index % 2 for index in range(20)]
        adopt = decide_experiment(control, [20 + index % 2 for index in range(20)], 7, guardrails(), guardrails())
        self.assertEqual(adopt.status, ExperimentStatus.ADOPT)
        rejected = decide_experiment(control, [20] * 20, 7, guardrails(), guardrails(3, 0.5, 2))
        self.assertEqual(rejected.status, ExperimentStatus.REJECT)
        inconclusive = decide_experiment(control, [11 + index % 2 for index in range(20)], 7, guardrails(), guardrails())
        self.assertEqual(inconclusive.status, ExperimentStatus.INCONCLUSIVE)

    def test_next_experiment_and_one_active_only(self):
        self.assertEqual(select_next_experiment(Bottleneck.DISTRIBUTION, [], None), "POST_INTENT")
        self.assertEqual(select_next_experiment(Bottleneck.DISTRIBUTION, ["POST_INTENT"], None), "TOPIC_RELEVANCE")
        self.assertIsNone(select_next_experiment(Bottleneck.DISTRIBUTION, [], "active"))
        self.assertEqual(EXPERIMENT_QUEUES[Bottleneck.CLICK][0], "PRODUCT_FRAMING")
        identifier = start_next_experiment(
            self.database.connection, "POST_INTENT", Bottleneck.DISTRIBUTION, self.now
        )
        self.assertTrue(identifier.startswith("post_intent-"))
        with self.assertRaises(RuntimeError):
            start_next_experiment(
                self.database.connection, "TOPIC_RELEVANCE", Bottleneck.DISTRIBUTION, self.now
            )

    def test_policy_versioning_and_allowlist(self):
        target = apply_policy_mutation(
            self.database.connection, field_name="question_presence", new_value="always",
            experiment_id="exp-1", reason="robust uplift", sample_size=40,
            effect_size=0.5, baseline_metrics=guardrails(), now=self.now,
        )
        self.assertEqual(target, "growth_policy_v2")
        self.assertEqual(current_policy_version(self.database.connection), target)
        row = self.database.connection.execute("SELECT * FROM growth_policy_history").fetchone()
        self.assertEqual(row["field_name"], "question_presence")
        self.assertEqual(row["sample_size"], 40)
        self.assertTrue(set(json.loads(self.database.connection.execute(
            "SELECT values_json FROM growth_policies WHERE policy_version=?", (target,)
        ).fetchone()[0])) <= MUTATION_ALLOWLIST)

    def test_denylist_and_hard_constraints(self):
        for field in ("cron", "deal_score_threshold", "affiliate_disclosure", "source_code", "db_schema"):
            with self.assertRaises(ValueError):
                validate_mutation(field, "changed")
        with self.assertRaises(ValueError):
            validate_mutation("text_length_target", 501)
        with self.assertRaises(ValueError):
            validate_mutation("topic_tag_candidate", "disable_relevance_threshold")
        self.assertEqual(THREADS_PUBLISHING.daily_post_limit, 4)
        self.assertEqual(THREADS_PUBLISHING.cycle_post_limit, 1)
        self.assertEqual(THREADS_PUBLISHING.minimum_deal_score, 75)

    def test_rollback_and_no_one_day_rollback(self):
        apply_policy_mutation(
            self.database.connection, field_name="question_presence", new_value="always",
            experiment_id="exp-1", reason="test", sample_size=40,
            effect_size=0.5, baseline_metrics=guardrails(), now=self.now - timedelta(days=8),
        )
        target = rollback_policy(self.database.connection, "commission deteriorated", guardrails(3), self.now)
        self.assertEqual(target, "growth_policy_v1")
        self.assertEqual(self.database.connection.execute("SELECT COUNT(*) FROM growth_rollbacks").fetchone()[0], 1)
        apply_policy_mutation(
            self.database.connection, field_name="question_presence", new_value="never",
            experiment_id="exp-2", reason="test", sample_size=40,
            effect_size=0.5, baseline_metrics=guardrails(), now=self.now,
        )
        with self.assertRaises(RuntimeError):
            rollback_policy(self.database.connection, "one day noise", guardrails(3), self.now + timedelta(days=1))

    def test_persistent_low_and_stage_progression(self):
        self.assertTrue(persistent_distribution_low(Bottleneck.DISTRIBUTION, 4, 10))
        self.assertFalse(persistent_distribution_low(Bottleneck.DISTRIBUTION, 3, 10))
        self.assertFalse(persistent_distribution_low(Bottleneck.ENGAGEMENT, 4, 10))
        self.assertNotIn("CTA", EXPERIMENT_QUEUES[Bottleneck.DISTRIBUTION])

    def test_revenue_guardrail(self):
        result = guardrails_acceptable(guardrails(10, 2, 8), guardrails(4, 1, 3))
        self.assertFalse(result.acceptable)
        self.assertIn("deteriorated", result.reason)

    def test_shadow_has_zero_mutation(self):
        before = tuple(self.database.connection.execute("SELECT * FROM growth_policy_history"))
        decision = BoundedGrowthController(self.database.connection, ControllerMode.SHADOW.value).evaluate(self.now)
        after = tuple(self.database.connection.execute("SELECT * FROM growth_policy_history"))
        self.assertFalse(decision.mutation_applied)
        self.assertEqual(before, after)
        self.assertEqual(GROWTH_OPTIMIZER_MODE, "shadow")
        self.assertEqual(AUTO_REPLY_MODE, "shadow")

    def test_not_eligible_without_completed_audited_experiment(self):
        result = bounded_auto_eligibility(self.database.connection)
        self.assertFalse(result.eligible)
        self.assertEqual(result.completed_experiments, 0)


if __name__ == "__main__":
    unittest.main()
