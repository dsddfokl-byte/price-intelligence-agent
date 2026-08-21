"""Normal-cycle comic assignment, delivery, and fallback regression tests."""

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

from app.comic_cycle import ComicPlan, publish_with_comic_plan
from app.comics.media_hosting import HostedComicAsset
from app.comics.stock_selector import COMIC, NO_COMIC, ComicSelection, assign_media_variant
from app.config import (
    COMIC_MEDIA_EXPERIMENT_EPOCH,
    COMIC_MEDIA_HOSTING_PROVIDER,
    COMIC_MEDIA_MIN_SAMPLE,
    COMIC_STOCK_PUBLISHING_ENABLED,
    THREADS_IMAGE_CONTAINER_TEST_ENABLED,
    THREADS_PUBLISHING,
)
from app.models import Product
from app.publishers.threads import ThreadsAPIError, ThreadsCandidate


NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)


def candidate() -> ThreadsCandidate:
    product = Product(
        item_code="shop:item", item_name="猫砂 6L", item_price=1980,
        shop_code="shop", shop_name="shop", item_url="https://example.invalid/item",
        affiliate_url="https://example.invalid/affiliate", review_average=4.7,
        review_count=100, affiliate_rate=4.0, availability=1,
        fetched_at=NOW.isoformat(),
    )
    text = "猫砂 6L\n確認時価格：1,980円\nhttps://example.invalid/affiliate\n※本投稿にはアフィリエイトリンクが含まれます。"
    return ThreadsCandidate(product, 80.0, text, "hash", "eligible", "猫砂", "猫", "PRICE_CONTROL", None, None)


def comic_plan(assigned: str = COMIC) -> ComicPlan:
    selection = ComicSelection(
        "comic_001" if assigned == COMIC else None,
        Path("comic_001.png") if assigned == COMIC else None,
        "comic_stock_v1", 150.0 if assigned == COMIC else 0.0,
        "category=100" if assigned == COMIC else "NO_COMIC experiment arm",
        None, "AVAILABLE" if assigned == COMIC else "NOT_APPLICABLE", assigned,
    )
    return ComicPlan(selection, "CLEANING")


def hosted() -> HostedComicAsset:
    return HostedComicAsset(
        "comic_001", "github_pages", "https://example.com/comic_001.png",
        "a" * 64, NOW, None, "PUBLIC_URL_OK",
    )


class ComicCycleTests(unittest.TestCase):
    def test_production_flags_and_limits(self) -> None:
        self.assertTrue(COMIC_STOCK_PUBLISHING_ENABLED)
        self.assertEqual(COMIC_MEDIA_HOSTING_PROVIDER, "github_pages")
        self.assertFalse(THREADS_IMAGE_CONTAINER_TEST_ENABLED)
        self.assertEqual(COMIC_MEDIA_EXPERIMENT_EPOCH, "v1")
        self.assertEqual(COMIC_MEDIA_MIN_SAMPLE, 10)
        self.assertEqual(THREADS_PUBLISHING.minimum_deal_score, 75)
        self.assertEqual(THREADS_PUBLISHING.daily_post_limit, 4)
        self.assertEqual(THREADS_PUBLISHING.cycle_post_limit, 1)

    def test_assignment_is_stable_balanced_and_independent(self) -> None:
        self.assertEqual(assign_media_variant("shop:item", NOW.date()), assign_media_variant("shop:item", NOW.date()))
        values = [assign_media_variant(f"shop:{index}", NOW.date()) for index in range(1000)]
        share = values.count(COMIC) / len(values)
        self.assertGreater(share, 0.45)
        self.assertLess(share, 0.55)

    def test_no_comic_uses_existing_text_path(self) -> None:
        publisher = Mock()
        publisher.publish_text.return_value = "post-text"
        publisher.get_post_details.return_value = {"permalink": "https://threads.net/post"}
        result = publish_with_comic_plan(publisher, candidate(), comic_plan(NO_COMIC))
        self.assertEqual(result.delivered_media_variant, NO_COMIC)
        publisher.publish_text.assert_called_once()
        publisher.create_image_container.assert_not_called()

    def test_comic_success_publishes_finished_container_once(self) -> None:
        publisher = Mock()
        publisher.create_image_container.return_value = "container"
        publisher.get_container_status.return_value = "FINISHED"
        publisher.publish_finished_container_once.return_value = "post-comic"
        publisher.get_post_details.return_value = {"permalink": "https://threads.net/comic"}
        # isinstance is deliberately satisfied with a real-provider-spec mock.
        from app.comics.media_hosting import GitHubPagesComicMediaHostingProvider
        provider = Mock(spec=GitHubPagesComicMediaHostingProvider)
        provider.resolve_asset.return_value = hosted()
        result = publish_with_comic_plan(publisher, candidate(), comic_plan(), provider=provider)
        self.assertEqual(result.delivered_media_variant, COMIC)
        publisher.publish_finished_container_once.assert_called_once_with("container", "FINISHED")
        publisher.publish_text.assert_not_called()

    def test_pre_publish_media_failure_falls_back_to_text(self) -> None:
        publisher = Mock()
        publisher.create_image_container.side_effect = ThreadsAPIError("safe create error", stage="image create", status_code=400)
        publisher.publish_text.return_value = "post-text"
        publisher.get_post_details.return_value = {}
        from app.comics.media_hosting import GitHubPagesComicMediaHostingProvider
        provider = Mock(spec=GitHubPagesComicMediaHostingProvider)
        provider.resolve_asset.return_value = hosted()
        result = publish_with_comic_plan(publisher, candidate(), comic_plan(), provider=provider)
        self.assertEqual(result.assigned_media_variant, COMIC)
        self.assertEqual(result.delivered_media_variant, NO_COMIC)
        self.assertEqual(result.fallback_reason, "COMIC_MEDIA_FAILED")
        publisher.publish_text.assert_called_once()

    def test_ambiguous_publish_is_not_retried_or_fallen_back(self) -> None:
        publisher = Mock()
        publisher.create_image_container.return_value = "container"
        publisher.get_container_status.return_value = "FINISHED"
        publisher.publish_finished_container_once.side_effect = ThreadsAPIError("ambiguous", stage="publish")
        from app.comics.media_hosting import GitHubPagesComicMediaHostingProvider
        provider = Mock(spec=GitHubPagesComicMediaHostingProvider)
        provider.resolve_asset.return_value = hosted()
        with self.assertRaises(ThreadsAPIError):
            publish_with_comic_plan(publisher, candidate(), comic_plan(), provider=provider)
        publisher.publish_finished_container_once.assert_called_once()
        publisher.publish_text.assert_not_called()


if __name__ == "__main__":
    unittest.main()
