"""Phase 1 growth diagnostics and shadow experiment tests."""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import (
    COMIC_MEDIA_EXPERIMENT_EPOCH,
    COMIC_MEDIA_HOSTING_PROVIDER,
    COMIC_STOCK_PUBLISHING_ENABLED,
    GROWTH_MIN_PRACTICAL_UPLIFT,
    GROWTH_MIN_RUNTIME_DAYS,
    GROWTH_MIN_SAMPLES_PER_ARM,
    GROWTH_OPTIMIZER_MODE,
    THREADS_PUBLISHING,
)
from app.database import Database
from app.growth_optimizer import (
    MUTATION_ALLOWLIST,
    MUTATION_DENYLIST,
    Bottleneck,
    ExperimentStatus,
    GrowthExperimentRegistry,
    compute_window_metrics,
    diagnose,
    determine_bottleneck,
    evaluate_views_experiment,
)
from app.init import initialize_database


NOW = datetime(2026, 8, 22, 12, tzinfo=timezone.utc)


class GrowthOptimizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "growth.db")
        initialize_database(self.database)
        self.database.connection.execute(
            """
            INSERT INTO products(item_code, first_seen_at, last_seen_at)
            VALUES ('shop:item', ?, ?)
            """,
            (NOW.isoformat(), NOW.isoformat()),
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def post(
        self,
        index: int,
        *,
        views=None,
        replies=None,
        reposts=None,
        clicks=None,
        orders=None,
        commission=None,
        media="NO_COMIC",
    ) -> None:
        posted_at = (NOW - timedelta(hours=index + 1)).isoformat()
        self.database.connection.execute(
            """
            INSERT INTO threads_posts(
                item_code, threads_post_id, posted_at, deal_score, text_hash,
                status, views, replies, reposts, clicks, confirmed_orders,
                confirmed_commission, delivered_media_variant, comic_id,
                search_keyword, topic_tag, template_variant
            ) VALUES ('shop:item', ?, ?, 75, ?, 'published', ?, ?, ?, ?, ?, ?, ?, ?,
                      '猫砂', '猫', 'PRICE_CONTROL')
            """,
            (
                f"post-{index}", posted_at, f"hash-{index}", views, replies,
                reposts, clicks, orders, commission, media,
                "comic_001" if media == "COMIC" else None,
            ),
        )

    def test_low_views_choose_distribution(self) -> None:
        for index, views in enumerate((5, 10, 15, 20, 25)):
            self.post(index, views=views, replies=0, reposts=0)
        diagnosis = diagnose(self.database.connection, NOW)
        self.assertEqual(diagnosis.current_bottleneck, Bottleneck.DISTRIBUTION)
        self.assertEqual(diagnosis.windows[7].distribution.median, 15)

    def test_distribution_clear_moves_to_engagement(self) -> None:
        for index in range(5):
            self.post(index, views=100, replies=0, reposts=0)
        metrics = compute_window_metrics(self.database.connection, NOW, 7)
        bottleneck, _ = determine_bottleneck(metrics)
        self.assertEqual(bottleneck, Bottleneck.ENGAGEMENT)

    def test_null_metrics_remain_null_and_dimensions_are_available(self) -> None:
        self.post(1, views=20, media="COMIC")
        metrics = compute_window_metrics(self.database.connection, NOW, 7)
        self.assertIsNone(metrics.totals["clicks"])
        self.assertIsNone(metrics.totals["confirmed_orders"])
        self.assertIsNone(metrics.totals["confirmed_commission"])
        self.assertEqual(metrics.dimensions["media_variant"]["COMIC"].posts, 1)
        self.assertIn("猫砂", metrics.dimensions["category"])
        self.assertIn("猫", metrics.dimensions["topic_tag"])
        self.assertTrue(metrics.dimensions["posting_slot"])

    def test_minimum_sample_and_runtime_prevent_early_winner(self) -> None:
        small = evaluate_views_experiment([100] * 19, [200] * 19, 14)
        short = evaluate_views_experiment([100] * 20, [200] * 20, 6.9)
        self.assertEqual(small.status, ExperimentStatus.INSUFFICIENT_SAMPLE)
        self.assertEqual(short.status, ExperimentStatus.INSUFFICIENT_SAMPLE)

    def test_practical_uplift_and_robust_evaluation(self) -> None:
        control = [90, 95, 100, 105, 110] * 4
        twenty = evaluate_views_experiment(
            control, [value * 1.2 for value in control], 7
        )
        fifty = evaluate_views_experiment(
            control, [value * 1.5 for value in control], 7
        )
        hundred = evaluate_views_experiment(
            control, [value * 2 for value in control], 7
        )
        self.assertEqual(twenty.status, ExperimentStatus.INCONCLUSIVE)
        self.assertEqual(fifty.status, ExperimentStatus.ADOPT)
        self.assertEqual(hundred.status, ExperimentStatus.ADOPT)
        self.assertEqual(
            fifty.bootstrap_ci,
            evaluate_views_experiment(
                control, [value * 1.5 for value in control], 7
            ).bootstrap_ci,
        )

    def test_one_viral_outlier_does_not_win(self) -> None:
        result = evaluate_views_experiment(
            [100] * 20, [100] * 19 + [10_000], 7
        )
        self.assertNotEqual(result.status, ExperimentStatus.ADOPT)
        self.assertEqual(result.treatment_median, 100)
        self.assertEqual(result.treatment_trimmed_mean, 100)

    def test_registry_is_planned_and_does_not_start_production(self) -> None:
        registry = GrowthExperimentRegistry(self.database.connection)
        identifier = registry.create_planned(
            "hook_template", "CURRENT", "QUESTION", created_at=NOW,
            experiment_id="growth-test-1",
        )
        self.assertEqual(identifier, "growth-test-1")
        row = registry.active()
        self.assertEqual(row["status"], "PLANNED")
        self.assertIsNone(row["started_at"])
        self.assertEqual(row["minimum_samples_per_arm"], 20)
        self.assertEqual(row["minimum_runtime_days"], 7)

    def test_shadow_diagnosis_has_no_production_side_effects(self) -> None:
        self.post(1, views=10)
        before = tuple(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM " + table
            ).fetchone()[0]
            for table in ("threads_posts", "comic_usage", "experiment_cycles")
        )
        diagnosis = diagnose(self.database.connection, NOW)
        after = tuple(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM " + table
            ).fetchone()[0]
            for table in ("threads_posts", "comic_usage", "experiment_cycles")
        )
        self.assertEqual(before, after)
        self.assertEqual(diagnosis.mode, "shadow")

    def test_mutation_policy_and_production_invariants(self) -> None:
        self.assertIn("hook_template", MUTATION_ALLOWLIST)
        self.assertIn("comic_selection_weight", MUTATION_ALLOWLIST)
        self.assertIn("deal_score_threshold", MUTATION_DENYLIST)
        self.assertIn("cron", MUTATION_DENYLIST)
        self.assertIn("source_code", MUTATION_DENYLIST)
        self.assertEqual(GROWTH_OPTIMIZER_MODE, "shadow")
        self.assertEqual(GROWTH_MIN_SAMPLES_PER_ARM, 20)
        self.assertEqual(GROWTH_MIN_RUNTIME_DAYS, 7)
        self.assertEqual(GROWTH_MIN_PRACTICAL_UPLIFT, 0.30)
        self.assertEqual(THREADS_PUBLISHING.daily_post_limit, 4)
        self.assertEqual(THREADS_PUBLISHING.cycle_post_limit, 1)
        self.assertEqual(THREADS_PUBLISHING.minimum_deal_score, 75)
        self.assertTrue(COMIC_STOCK_PUBLISHING_ENABLED)
        self.assertEqual(COMIC_MEDIA_HOSTING_PROVIDER, "github_pages")
        self.assertEqual(COMIC_MEDIA_EXPERIMENT_EPOCH, "v1")


if __name__ == "__main__":
    unittest.main()
