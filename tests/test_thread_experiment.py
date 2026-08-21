"""Regression tests for the two-variant Threads content experiment."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from app.collector import load_search_terms
from app.config import SEARCH_TERMS_PATH, THREADS_PUBLISHING, THREADS_TOPIC_TAGS
from app.database import Database
from app.models import Product
from app.publishers.threads import ThreadsAPIError, ThreadsPublisher, evaluate_product
from app.publishers.title_formatter import shorten_product_title
from app.thread_content import (
    OWNER_VALUE,
    PRICE_CONTROL,
    assign_variant,
    build_content_trigger,
    generate_experiment_text,
    load_owner_tips,
)


NOW = datetime(2026, 8, 16, 3, 0, tzinfo=timezone.utc)
URL = "https://example.invalid/affiliate-item"
FORBIDDEN = (
    "使ってみた",
    "うちの猫が愛用",
    "これしか食べない",
    "私のお気に入り",
    "病気",
    "治療",
    "予防",
    "免疫",
    "整腸",
    "関節改善",
    "皮膚改善",
    "アレルギー改善",
    "寿命",
)


def product() -> Product:
    return Product(
        item_code="shop:item",
        item_name="テストブランド 猫砂 6L×3個セット",
        item_price=1980,
        shop_code="shop",
        shop_name="shop",
        item_url="https://example.invalid/item",
        affiliate_url=URL,
        review_average=4.7,
        review_count=1026,
        affiliate_rate=4.0,
        availability=1,
        fetched_at=NOW.isoformat(),
        point_rate=5,
        postage_flag=0,
    )


class ThreadExperimentTests(unittest.TestCase):
    def test_deterministic_assignment_is_balanced(self) -> None:
        first = assign_variant("shop:item", NOW)
        self.assertEqual(first, assign_variant("shop:item", NOW))
        assignments = [assign_variant(f"shop:{index}", NOW) for index in range(1000)]
        owner_share = assignments.count(OWNER_VALUE) / len(assignments)
        self.assertGreater(owner_share, 0.45)
        self.assertLess(owner_share, 0.55)

    def test_topic_mapping_and_daily_limit_are_unchanged(self) -> None:
        self.assertEqual(
            THREADS_TOPIC_TAGS,
            {
                "猫 フード": "猫", "猫砂": "猫", "ペットシーツ": "ペット",
                "犬 フード": "犬", "犬 おやつ": "犬", "猫 おやつ": "猫",
                "犬 おもちゃ": "犬", "猫 おもちゃ": "猫",
                "ペット 自動給餌器": "ペット", "ペット 給水器": "ペット",
                "ペットカメラ": "ペット", "ペット トイレ": "ペット",
            },
        )
        self.assertEqual(THREADS_PUBLISHING.daily_post_limit, 4)
        self.assertEqual(THREADS_PUBLISHING.cycle_post_limit, 1)

    def test_all_search_terms_have_topic_tags_and_owner_tips(self) -> None:
        search_terms = load_search_terms(SEARCH_TERMS_PATH)
        self.assertEqual(len(search_terms), 12)
        self.assertEqual(set(search_terms), set(THREADS_TOPIC_TAGS))
        tips = load_owner_tips()
        for category in search_terms:
            self.assertGreaterEqual(
                len([tip for tip in tips if tip.category == category]), 3
            )

    def test_both_templates_are_safe_and_complete(self) -> None:
        tip = next(item for item in load_owner_tips() if item.category == "猫砂")
        for variant, selected_tip in ((PRICE_CONTROL, None), (OWNER_VALUE, tip)):
            text = generate_experiment_text(
                product(), 76.7, "猫砂", variant, selected_tip, "送料込み"
            )
            self.assertLessEqual(len(text), 500)
            self.assertIn(URL, text)
            self.assertNotIn("【PR】", text)
            self.assertIn("確認時価格", text)
            self.assertTrue(
                text.endswith("※本投稿にはアフィリエイトリンクが含まれます。")
            )
            for phrase in FORBIDDEN:
                self.assertNotIn(phrase, text)

    def test_pr_label_is_explicitly_opt_in_for_both_templates(self) -> None:
        tip = next(item for item in load_owner_tips() if item.category == "猫砂")
        for variant, selected_tip in ((PRICE_CONTROL, None), (OWNER_VALUE, tip)):
            normal = generate_experiment_text(
                product(), 76.7, "猫砂", variant, selected_tip, None,
                requires_pr_label=False,
            )
            labelled = generate_experiment_text(
                product(), 76.7, "猫砂", variant, selected_tip, None,
                requires_pr_label=True,
            )
            self.assertFalse(normal.startswith("【PR】"))
            self.assertTrue(labelled.startswith("【PR】"))
            for text in (normal, labelled):
                self.assertIn(URL, text)
                self.assertTrue(
                    text.endswith("※本投稿にはアフィリエイトリンクが含まれます。")
                )
                self.assertLessEqual(len(text), 500)

    def test_trigger_uses_only_product_data(self) -> None:
        self.assertEqual(build_content_trigger(product(), 2200), "前回チェックより10.0%下落")
        self.assertEqual(build_content_trigger(product(), 1980), "ポイント5倍")

    def test_trigger_icons_and_owner_heading(self) -> None:
        tip = next(item for item in load_owner_tips() if item.category == "猫砂")
        cases = (
            ("前回チェックより2.4%下落", "📉"),
            ("ポイント3倍", "🎁"),
            ("送料込み", "📦"),
            ("レビュー2,000件以上", "⭐"),
            ("確認ポイント", "💡"),
        )
        for trigger, icon in cases:
            for variant, selected_tip in ((PRICE_CONTROL, None), (OWNER_VALUE, tip)):
                text = generate_experiment_text(
                    product(), 76.7, "猫砂", variant, selected_tip, trigger
                )
                self.assertIn(f"{icon} {trigger}", text)
                self.assertNotIn("📡", text)
        owner_text = generate_experiment_text(
            product(), 76.7, "猫砂", OWNER_VALUE, tip, cases[0][0]
        )
        self.assertIn("\n今日チェックした商品\n", owner_text)

    def test_promotional_title_removal_preserves_product_details(self) -> None:
        self.assertEqual(
            shorten_product_title(
                "【圧倒的高評価！】ペットシーツ 洗える 猫 犬 おしっこパッド"
            ),
            "ペットシーツ 洗える 猫 犬 おしっこパッド",
        )
        self.assertEqual(
            shorten_product_title(
                "【ランキング1位】【送料無料】ニュートロ シュプレモ 小型犬 成犬用 3kg"
            ),
            "ニュートロ シュプレモ 小型犬 成犬用 3kg",
        )

    def test_tip_reuse_query_counts_only_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db")
            database.initialize()
            database.connection.execute(
                "INSERT INTO products(item_code, first_seen_at, last_seen_at) VALUES (?, ?, ?)",
                ("shop:item", NOW.isoformat(), NOW.isoformat()),
            )
            database.connection.commit()
            database.record_threads_post(
                "shop:item", None, NOW.isoformat(), 80, 100, "failed", "failed",
                tip_id="tip-a",
            )
            self.assertFalse(database.has_published_tip_since("tip-a", "2026-07-17"))
            database.record_threads_post(
                "shop:item", "post", NOW.isoformat(), 80, 100, "published", "published",
                tip_id="tip-a",
            )
            self.assertTrue(database.has_published_tip_since("tip-a", "2026-07-17"))
            database.close()

    def test_topic_4xx_falls_back_once(self) -> None:
        publisher = ThreadsPublisher("test-token")
        publisher._create_text_container_once = Mock(
            side_effect=[
                ThreadsAPIError("safe", stage="create", status_code=400),
                "creation-id",
            ]
        )
        self.assertEqual(
            publisher.create_text_container("safe text", topic_tag="猫"),
            "creation-id",
        )
        self.assertEqual(
            publisher._create_text_container_once.call_args_list[0].args,
            ("safe text", "猫"),
        )
        self.assertEqual(
            publisher._create_text_container_once.call_args_list[1].args,
            ("safe text", None),
        )
        publisher.close()

    def test_topic_5xx_does_not_add_a_create_fallback(self) -> None:
        publisher = ThreadsPublisher("test-token")
        publisher._create_text_container_once = Mock(
            side_effect=ThreadsAPIError("safe", stage="create", status_code=500)
        )
        with self.assertRaises(ThreadsAPIError):
            publisher.create_text_container("safe text", topic_tag="猫")
        self.assertEqual(publisher._create_text_container_once.call_count, 1)
        publisher.close()

    def test_existing_duplicate_and_published_only_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db")
            database.initialize()
            strong = Product(
                **{
                    **product().__dict__,
                    "affiliate_rate": 10.0,
                    "review_average": 5.0,
                    "review_count": 10000,
                }
            )
            database.save_products([strong])
            database.save_product_keywords("猫砂", [strong.item_code], strong.fetched_at)
            row = database.product_for_threads(strong.item_code)
            candidate, _ = evaluate_product(database, row, now=NOW)
            self.assertIsNotNone(candidate)
            database.record_threads_post(
                strong.item_code, None, NOW.isoformat(), 100, strong.item_price,
                candidate.text_hash, "failed",
            )
            candidate_after_failure, _ = evaluate_product(database, row, now=NOW)
            self.assertIsNotNone(candidate_after_failure)
            self.assertEqual(database.published_threads_count_since("2026-08-16"), 0)
            database.record_threads_post(
                strong.item_code, "post", NOW.isoformat(), 100, strong.item_price,
                candidate.text_hash, "published",
            )
            blocked, reason = evaluate_product(database, row, now=NOW)
            self.assertIsNone(blocked)
            self.assertIn("7日以内", reason)
            self.assertEqual(database.published_threads_count_since("2026-08-16"), 1)
            database.close()


if __name__ == "__main__":
    unittest.main()
