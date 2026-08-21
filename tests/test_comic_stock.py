"""Validation, selection, and non-critical delivery tests for comic stock."""

import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from app.comics.stock_selector import (
    COMIC,
    NO_COMIC,
    ComicStockSelector,
    ComicUsageRecord,
    assign_media_variant,
)
from app.comics.stock_validator import load_and_validate_manifest
from app.config import (
    COMIC_STOCK_ENABLED,
    COMIC_STOCK_MANIFEST_PATH,
    COMIC_STOCK_PUBLISHING_ENABLED,
    PROJECT_ROOT,
    THREADS_PUBLISHING,
)
from app.database import Database
from app.autopilot import get_state
from app.comics.media_publisher import ComicThreadsPublisher
from app.publishers.threads import ThreadsAPIError
from app.thread_content import assign_variant


TODAY = date(2026, 8, 21)


class ComicStockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.payload = json.loads(COMIC_STOCK_MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.validation = load_and_validate_manifest(COMIC_STOCK_MANIFEST_PATH)
        cls.selector = ComicStockSelector()

    def write_manifest(self, payload: dict, directory: str) -> Path:
        path = Path(directory) / "manifest.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_stock_has_exactly_50_valid_numbered_png_assets(self) -> None:
        self.assertEqual(len(self.validation.assets), 50)
        self.assertEqual(len(self.validation.valid_assets), 50)
        self.assertFalse(self.validation.errors)
        self.assertEqual(
            [asset.comic_id for asset in self.validation.assets],
            [f"comic_{index:03d}" for index in range(1, 51)],
        )
        self.assertTrue(all(asset.file_path.suffix.lower() == ".png" for asset in self.validation.assets))
        for category in (
            "猫 フード", "猫砂", "ペットシーツ", "犬 フード", "犬 おやつ", "猫 おやつ",
            "犬 おもちゃ", "猫 おもちゃ", "ペット 自動給餌器", "ペット 給水器",
            "ペットカメラ", "ペット トイレ",
        ):
            self.assertGreaterEqual(
                sum(category in asset.category_scores for asset in self.validation.assets), 2
            )

    def test_duplicate_id_and_file_are_rejected(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["items"][1]["comic_id"] = payload["items"][0]["comic_id"]
        payload["items"][2]["file"] = payload["items"][0]["file"]
        with tempfile.TemporaryDirectory() as directory:
            result = load_and_validate_manifest(self.write_manifest(payload, directory))
        self.assertTrue(any("duplicate comic_id" in error for error in result.errors))
        self.assertTrue(any("duplicate file" in error for error in result.errors))

    def test_missing_and_hash_mismatch_disable_only_affected_assets(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["items"][0]["file"] = "01_missing.png"
        payload["items"][1]["sha256"] = "0" * 64
        with tempfile.TemporaryDirectory() as directory:
            result = load_and_validate_manifest(self.write_manifest(payload, directory))
        self.assertTrue(any("COMIC_ASSET_MISSING" in error for error in result.errors))
        self.assertTrue(any("COMIC_ASSET_HASH_MISMATCH" in error for error in result.errors))
        self.assertGreaterEqual(len(result.valid_assets), 48)

    def test_invalid_category_mapping_is_rejected(self) -> None:
        payload = copy.deepcopy(self.payload)
        payload["items"][0]["category_scores"]["UNKNOWN"] = 100
        with tempfile.TemporaryDirectory() as directory:
            result = load_and_validate_manifest(self.write_manifest(payload, directory))
        self.assertTrue(any("invalid category mapping" in error for error in result.errors))

    def select(self, **overrides: object):
        values = dict(
            item_code="shop:item", search_keyword="猫砂", category="猫砂",
            topic_tag="猫", post_type="PRICE_CONTROL", posting_date=TODAY,
            usages=(), assigned_media_variant=COMIC,
        )
        values.update(overrides)
        return self.selector.select(**values)

    def test_selection_is_deterministic_and_enforces_cooldown_and_product_reuse(self) -> None:
        first = self.select()
        self.assertEqual(first, self.select())
        usage = ComicUsageRecord(
            first.comic_id, "other:item", "猫砂",
            datetime(2026, 8, 20, tzinfo=timezone.utc),
        )
        second = self.select(usages=(usage,))
        self.assertNotEqual(first.comic_id, second.comic_id)
        old_same_product = ComicUsageRecord(
            first.comic_id, "shop:item", "猫砂",
            datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        self.assertNotEqual(first.comic_id, self.select(usages=(old_same_product,)).comic_id)

    def test_secondary_global_fallback_and_no_candidate(self) -> None:
        fallback = self.select(category="ペット 自動給餌器", search_keyword="ペット 自動給餌器", topic_tag="ペット")
        self.assertIsNotNone(fallback.comic_id)
        usages = tuple(
            ComicUsageRecord(
                asset.comic_id, "other", "猫砂",
                datetime(2026, 8, 20, tzinfo=timezone.utc),
            )
            for asset in self.validation.valid_assets
        )
        none = self.select(usages=usages)
        self.assertIsNone(none.comic_id)
        self.assertEqual(none.delivered_media_variant, NO_COMIC)

    def test_media_assignment_is_stable_and_independent_of_text_variant(self) -> None:
        first = assign_media_variant("shop:item", TODAY)
        self.assertEqual(first, assign_media_variant("shop:item", TODAY))
        self.assertIn(first, (COMIC, NO_COMIC))
        self.assertEqual(assign_variant("shop:item", datetime(2026, 8, 21, tzinfo=timezone.utc)), assign_variant("shop:item", datetime(2026, 8, 21, tzinfo=timezone.utc)))

    def test_assigned_and_delivered_are_separate_and_image_failure_falls_back(self) -> None:
        result = self.select()
        self.assertEqual(result.assigned_media_variant, COMIC)
        self.assertEqual(result.delivered_media_variant, NO_COMIC)
        publisher = ComicThreadsPublisher("test-token")
        publisher.publish_image = Mock(side_effect=ThreadsAPIError("safe", stage="image create"))
        publisher.publish_text = Mock(return_value="post-id")
        delivery = publisher.publish_with_optional_image(
            "safe text", "猫", "https://example.invalid/comic.png"
        )
        self.assertEqual(delivery.post_id, "post-id")
        self.assertEqual(delivery.delivered_media_variant, NO_COMIC)
        publisher.publish_text.assert_called_once()
        publisher.close()

    def test_image_container_uses_documented_public_url_parameters(self) -> None:
        publisher = ComicThreadsPublisher("test-token")
        publisher.get_user_id = Mock(return_value="user-id")
        publisher._request = Mock(return_value={"id": "creation-id"})
        self.assertEqual(
            publisher.create_image_container(
                "safe text",
                "https://example.invalid/comic.png",
                "猫と犬の日常を描いた縦4コマ漫画",
                "猫",
            ),
            "creation-id",
        )
        publisher._request.assert_called_once_with(
            "POST", "/user-id/threads", stage="image create",
            params={
                "media_type": "IMAGE",
                "image_url": "https://example.invalid/comic.png",
                "text": (
                    "safe text\n\n"
                    "※本投稿にはアフィリエイトリンクが含まれます。"
                ),
                "alt_text": "猫と犬の日常を描いた縦4コマ漫画",
                "topic_tag": "猫",
            },
        )
        publisher.close()

    def test_migration_is_idempotent_and_defaults_preserve_production(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db")
            database.initialize()
            database.initialize()
            state_before = get_state(database.connection)
            self.select()
            state_after = get_state(database.connection)
            self.assertEqual(database.connection.execute("SELECT COUNT(*) FROM comic_usage").fetchone()[0], 0)
            self.assertEqual(state_before, state_after)
            database.close()
        self.assertTrue(COMIC_STOCK_ENABLED)
        self.assertTrue(COMIC_STOCK_PUBLISHING_ENABLED)
        self.assertEqual(THREADS_PUBLISHING.minimum_deal_score, 75)
        self.assertEqual(THREADS_PUBLISHING.daily_post_limit, 2)


if __name__ == "__main__":
    unittest.main()
