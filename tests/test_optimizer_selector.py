"""Performance optimizer scoring, leakage, ranking, and integration tests."""

import math
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.autopilot import (
    AutopilotController,
    ExecutionState,
    ExperimentArm,
    RewardMode,
    get_state,
)
from app.config import THREADS_PUBLISHING
from app.database import Database
from app.models import Product
from app.optimizer_selector import (
    MAX_HISTORICAL_ADJUSTMENT,
    MIN_72H_OUTCOMES,
    OptimizerSelectorError,
    PerformanceOptimizerSelector,
    choose_candidate_for_arm,
    engagement_proxy,
    historical_adjustment,
    smooth_proxy,
)
from app.publishers.threads import ThreadsCandidate


NOW = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


def candidate(
    item_code: str,
    score: float,
    category: str = "猫砂",
    template: str = "PRICE_CONTROL",
    tip_id: str = "tip-a",
) -> ThreadsCandidate:
    product = Product(
        item_code=item_code,
        item_name=item_code,
        item_price=1000,
        shop_code="shop",
        shop_name="shop",
        item_url="https://example.invalid/item",
        affiliate_url="https://example.invalid/affiliate",
        review_average=4.8,
        review_count=100,
        affiliate_rate=4.0,
        availability=1,
        fetched_at=NOW.isoformat(),
    )
    return ThreadsCandidate(
        product=product,
        deal_score=score,
        text="【PR】商品\nhttps://example.invalid/affiliate\n※本投稿にはアフィリエイトリンクが含まれます。",
        text_hash=f"hash-{item_code}",
        reason="eligible",
        search_keyword=category,
        topic_tag="猫",
        template_variant=template,
        tip_id=tip_id,
        content_trigger=None,
    )


class OptimizerSelectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temp.name) / "optimizer.db")
        self.database.initialize()
        self.selector = PerformanceOptimizerSelector(self.database.connection)

    def tearDown(self) -> None:
        self.database.close()
        self.temp.cleanup()

    def add_outcome(
        self,
        index: int,
        *,
        category: str,
        template: str,
        views: int,
        actions: int,
        captured_at: datetime = NOW - timedelta(hours=1),
        valid: bool = True,
        topic: str = "unused-topic",
        tip: str = "unused-tip",
    ) -> None:
        item_code = f"history:{index}"
        post_id = f"post-{index}"
        with self.database.connection:
            self.database.connection.execute(
                "INSERT OR IGNORE INTO products(item_code,first_seen_at,last_seen_at) VALUES (?,?,?)",
                (item_code, NOW.isoformat(), NOW.isoformat()),
            )
            self.database.connection.execute(
                """
                INSERT INTO threads_posts(
                    item_code,threads_post_id,posted_at,deal_score,text_hash,status,
                    search_keyword,template_variant,topic_tag,tip_id
                ) VALUES (?,?,?,?,?,'published',?,?,?,?)
                """,
                (
                    item_code, post_id, (captured_at - timedelta(hours=72)).isoformat(),
                    80, f"hash-{index}", category, template, topic, tip,
                ),
            )
            self.database.connection.execute(
                """
                INSERT INTO post_performance_snapshots(
                    threads_post_id,captured_at,horizon_hours,is_valid_72h,
                    views,replies,reposts,quotes,shares
                ) VALUES (?,?,72,?,?,?,?,?,?)
                """,
                (
                    post_id, captured_at.isoformat(), int(valid), views,
                    actions, 0, 0, 0,
                ),
            )

    def add_balanced_history(self) -> None:
        for index in range(3):
            self.add_outcome(
                index, category="猫砂", template="OWNER_VALUE",
                views=1000, actions=10,
            )
        for index in range(3, 6):
            self.add_outcome(
                index, category="犬 フード", template="PRICE_CONTROL",
                views=100, actions=0,
            )

    def test_small_sample_has_zero_adjustment(self) -> None:
        self.add_outcome(
            1, category="猫砂", template="OWNER_VALUE", views=1000, actions=10
        )
        analysis = self.selector.analyze(
            [candidate("a", 80)], decided_at=NOW,
            reward_mode=RewardMode.ENGAGEMENT_PROXY,
        )
        self.assertLess(analysis.valid_outcomes, MIN_72H_OUTCOMES)
        self.assertFalse(analysis.adjustment_enabled)
        self.assertEqual(analysis.ranking[0].historical_adjustment, 0)
        self.assertEqual(analysis.ranking[0].optimizer_score, 80)

    def test_sufficient_category_adjustment_and_smoothing(self) -> None:
        self.add_balanced_history()
        analysis = self.selector.analyze(
            [candidate("a", 80, category="猫砂", template="UNKNOWN")],
            decided_at=NOW, reward_mode=RewardMode.ENGAGEMENT_PROXY,
        )
        item = analysis.ranking[0]
        self.assertEqual(item.category_n, 3)
        self.assertGreater(item.category_delta, 0)
        expected = smooth_proxy(
            analysis.category_stats["猫砂"].mean_proxy,
            3,
            analysis.global_mean_proxy,
        )
        self.assertAlmostEqual(item.category_smoothed_proxy, expected)
        self.assertGreater(item.historical_adjustment, 0)

    def test_sufficient_template_adjustment(self) -> None:
        self.add_balanced_history()
        analysis = self.selector.analyze(
            [candidate("a", 80, category="UNKNOWN", template="OWNER_VALUE")],
            decided_at=NOW, reward_mode=RewardMode.ENGAGEMENT_PROXY,
        )
        item = analysis.ranking[0]
        self.assertEqual(item.template_n, 3)
        self.assertGreater(item.template_delta, 0)
        self.assertGreater(item.historical_adjustment, 0)

    def test_adjustment_clamps(self) -> None:
        self.assertEqual(historical_adjustment([100]), MAX_HISTORICAL_ADJUSTMENT)
        self.assertEqual(historical_adjustment([-100]), -MAX_HISTORICAL_ADJUSTMENT)

    def test_deterministic_ranking_and_item_code_tie_break(self) -> None:
        pool = [candidate("z", 80), candidate("a", 80), candidate("m", 79)]
        first = self.selector.analyze(
            pool, decided_at=NOW, reward_mode=RewardMode.ENGAGEMENT_PROXY
        )
        second = self.selector.analyze(
            pool, decided_at=NOW, reward_mode=RewardMode.ENGAGEMENT_PROXY
        )
        expected = ["a", "z", "m"]
        self.assertEqual(
            [item.candidate.product.item_code for item in first.ranking], expected
        )
        self.assertEqual(first.ranking, second.ranking)

    def test_future_and_lookback_data_are_excluded(self) -> None:
        self.add_outcome(
            1, category="猫砂", template="OWNER_VALUE", views=1000, actions=10,
            captured_at=NOW + timedelta(seconds=1),
        )
        self.add_outcome(
            2, category="猫砂", template="OWNER_VALUE", views=1000, actions=10,
            captured_at=NOW - timedelta(days=31),
        )
        analysis = self.selector.analyze(
            [candidate("a", 80)], decided_at=NOW,
            reward_mode=RewardMode.ENGAGEMENT_PROXY,
        )
        self.assertEqual(analysis.valid_outcomes, 0)

    def test_topic_time_and_tip_do_not_affect_proxy(self) -> None:
        first = engagement_proxy(1000, 10, 0, 0, 0)
        self.add_outcome(
            1, category="猫砂", template="OWNER_VALUE", views=1000, actions=10,
            topic="topic-a", tip="tip-a",
        )
        outcome = self.selector.load_outcomes(NOW)[0]
        self.assertEqual(outcome.proxy, first)
        self.assertNotIn("topic", outcome.__dict__)
        self.assertNotIn("tip", outcome.__dict__)

    def test_pool_is_not_mutated_and_analysis_has_no_product_side_effects(self) -> None:
        pool = [candidate("b", 79), candidate("a", 80)]
        original_ids = [id(item) for item in pool]
        before = {
            table: self.database.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in (
                "threads_posts", "price_history", "products", "product_keywords"
            )
        }
        self.selector.analyze(
            pool, decided_at=NOW, reward_mode=RewardMode.ENGAGEMENT_PROXY
        )
        after = {
            table: self.database.connection.execute(
                f"SELECT COUNT(*) FROM {table}"
            ).fetchone()[0]
            for table in before
        }
        self.assertEqual(original_ids, [id(item) for item in pool])
        self.assertEqual(before, after)

    def test_shadow_control_and_optimizer_use_same_pool(self) -> None:
        pool = [candidate("production", 80), candidate("optimizer", 90)]
        analysis = self.selector.analyze(
            pool, decided_at=NOW, reward_mode=RewardMode.ENGAGEMENT_PROXY
        )
        shadow = get_state(self.database.connection)
        chosen, source = choose_candidate_for_arm(
            pool, analysis, shadow, ExperimentArm.OPTIMIZER
        )
        self.assertIs(chosen, pool[0])
        self.assertEqual(source, "CONTROL")

        controller = AutopilotController(self.database.connection)
        limited = controller.evaluate(
            "limited", now=NOW, requested_state=ExecutionState.LIMITED_LIVE
        )
        control, source = choose_candidate_for_arm(
            pool, analysis, limited, ExperimentArm.CONTROL
        )
        optimized, optimizer_source = choose_candidate_for_arm(
            pool, analysis, limited, ExperimentArm.OPTIMIZER
        )
        self.assertIs(control, pool[0])
        self.assertEqual(source, "CONTROL")
        self.assertIs(optimized, pool[1])
        self.assertEqual(optimizer_source, "OPTIMIZER")
        self.assertEqual({id(item) for item in pool}, {id(control), id(optimized)})

    def test_invalid_score_falls_back_but_empty_pool_is_normal(self) -> None:
        with self.assertRaises(OptimizerSelectorError):
            self.selector.analyze(
                [candidate("bad", math.nan)], decided_at=NOW,
                reward_mode=RewardMode.ENGAGEMENT_PROXY,
            )
        state = AutopilotController(self.database.connection).evaluate(
            "optimizer-error", now=NOW, optimizer_error="invalid score"
        )
        self.assertEqual(state.execution_state, ExecutionState.FALLBACK_CONTROL)
        empty = self.selector.analyze(
            [], decided_at=NOW, reward_mode=RewardMode.ENGAGEMENT_PROXY
        )
        chosen, source = choose_candidate_for_arm(
            [], empty, state, ExperimentArm.CONTROL
        )
        self.assertIsNone(chosen)
        self.assertEqual(source, "NO_CANDIDATE")

    def test_click_and_revenue_without_provider_have_zero_adjustment(self) -> None:
        self.add_balanced_history()
        pool = [candidate("a", 80)]
        for mode in (RewardMode.CLICK_PROXY, RewardMode.REVENUE):
            analysis = self.selector.analyze(pool, decided_at=NOW, reward_mode=mode)
            self.assertFalse(analysis.adjustment_enabled)
            self.assertEqual(analysis.ranking[0].historical_adjustment, 0)

    def test_existing_hard_configuration_is_unchanged(self) -> None:
        self.assertEqual(THREADS_PUBLISHING.daily_post_limit, 4)
        self.assertEqual(THREADS_PUBLISHING.cycle_post_limit, 1)
        self.assertEqual(THREADS_PUBLISHING.minimum_deal_score, 75)
        self.assertEqual(THREADS_PUBLISHING.item_cooldown_days, 7)
        self.assertEqual(THREADS_PUBLISHING.text_cooldown_days, 30)

    def test_insights_update_captures_valid_72h_snapshot(self) -> None:
        with self.database.connection:
            self.database.connection.execute(
                "INSERT INTO products(item_code,first_seen_at,last_seen_at) VALUES (?,?,?)",
                ("snapshot:item", NOW.isoformat(), NOW.isoformat()),
            )
            self.database.connection.execute(
                """
                INSERT INTO threads_posts(
                    item_code,threads_post_id,posted_at,deal_score,text_hash,status
                ) VALUES (?,?,?,?,?,'published')
                """,
                (
                    "snapshot:item", "snapshot-post",
                    (NOW - timedelta(hours=72)).isoformat(), 80, "snapshot-hash",
                ),
            )
        self.database.update_threads_insights(
            "snapshot-post", views=100, likes=1, replies=2, reposts=0,
            quotes=0, shares=0, updated_at=NOW.isoformat(),
        )
        row = self.database.connection.execute(
            "SELECT * FROM post_performance_snapshots WHERE threads_post_id='snapshot-post'"
        ).fetchone()
        self.assertEqual(row["is_valid_72h"], 1)
        self.assertAlmostEqual(row["horizon_hours"], 72.0)

    def test_dry_run_audits_do_not_create_experiment_evidence(self) -> None:
        pool = [candidate("audit", 80)]
        state = get_state(self.database.connection)
        analysis = self.selector.analyze(
            pool, decided_at=NOW, reward_mode=state.reward_mode
        )
        self.selector.persist_scores(
            analysis, cycle_id="dry-cycle", run_mode="dry_run", decided_at=NOW,
            reward_mode=state.reward_mode, experiment_epoch=state.experiment_epoch,
        )
        self.selector.persist_decision(
            cycle_id="dry-cycle", run_mode="dry_run", decided_at=NOW,
            production_item_code="audit", optimizer_choice=analysis.ranking[0],
            selected_item_code="audit", reward_mode=state.reward_mode,
            experiment_epoch=state.experiment_epoch,
            experiment_arm=ExperimentArm.CONTROL,
        )
        self.assertEqual(
            self.database.connection.execute(
                "SELECT COUNT(*) FROM experiment_cycles"
            ).fetchone()[0],
            0,
        )


if __name__ == "__main__":
    unittest.main()
