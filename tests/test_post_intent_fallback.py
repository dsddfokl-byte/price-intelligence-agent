import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.config import (
    COMIC_MEDIA_EXPERIMENT_EPOCH,
    POST_INTENT_EPOCH,
    POST_INTENT_SPLIT,
    THREADS_PUBLISHING,
    THREADS_TOPIC_TAGS,
)
from app.database import Database
from app.growth_content import validate_growth_text
from app.post_intent import PostIntent
from app.post_intent_delivery import (
    AFFILIATE_NO_ELIGIBLE_PRODUCT,
    prepare_growth_candidate,
    resolve_delivered_intent,
)


class FakeDatabase:
    def __init__(self, duplicate=False):
        self.duplicate = duplicate

    def has_published_text_hash_since(self, _text_hash, _since):
        return self.duplicate


class PostIntentFallbackTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 22, 17, 30, tzinfo=ZoneInfo("Asia/Tokyo"))
        self.terms = tuple(THREADS_TOPIC_TAGS)

    def test_affiliate_with_candidate_remains_affiliate(self):
        delivered, reason = resolve_delivered_intent(PostIntent.AFFILIATE, 1)
        self.assertEqual(delivered, PostIntent.AFFILIATE)
        self.assertIsNone(reason)

    def test_empty_affiliate_falls_back_without_changing_assignment(self):
        assigned = PostIntent.AFFILIATE
        delivered, reason = resolve_delivered_intent(assigned, 0)
        self.assertEqual(assigned, PostIntent.AFFILIATE)
        self.assertEqual(delivered, PostIntent.GROWTH)
        self.assertEqual(reason, AFFILIATE_NO_ELIGIBLE_PRODUCT)

    def test_growth_path_is_noncommercial_and_deal_score_independent(self):
        post, error = prepare_growth_candidate(
            FakeDatabase(), self.now, self.terms, dry_run=True
        )
        self.assertIsNone(error)
        self.assertEqual(post.deal_score, 0.0)
        self.assertIsNone(post.product.affiliate_url)
        self.assertNotIn("アフィリエイトリンク", post.text)
        validate_growth_text(post.text)

    def test_growth_duplicate_and_generation_failure_are_skips(self):
        post, error = prepare_growth_candidate(
            FakeDatabase(duplicate=True), self.now, self.terms, dry_run=True
        )
        self.assertIsNone(post)
        self.assertEqual(error, "DUPLICATE")
        with patch(
            "app.post_intent_delivery.generate_generic_growth_post",
            side_effect=ValueError("safe test failure"),
        ):
            post, error = prepare_growth_candidate(
                FakeDatabase(), self.now, self.terms, dry_run=True
            )
        self.assertIsNone(post)
        self.assertEqual(error, "GENERATION_FAILED")

    def test_database_records_itt_and_delivered_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "test.db") as database:
                database.initialize()
                database.record_threads_post(
                    item_code=None,
                    threads_post_id="post-1",
                    posted_at=self.now.isoformat(),
                    deal_score=0.0,
                    price=None,
                    text_hash="growth-fallback",
                    status="published",
                    post_intent="GROWTH",
                    assigned_post_intent="AFFILIATE",
                    delivered_post_intent="GROWTH",
                    post_intent_fallback_reason=AFFILIATE_NO_ELIGIBLE_PRODUCT,
                    post_intent_epoch=POST_INTENT_EPOCH,
                    growth_template="EMPATHY",
                )
                row = database.connection.execute(
                    "SELECT * FROM threads_posts WHERE threads_post_id='post-1'"
                ).fetchone()
                self.assertEqual(row["assigned_post_intent"], "AFFILIATE")
                self.assertEqual(row["delivered_post_intent"], "GROWTH")
                self.assertEqual(
                    row["post_intent_fallback_reason"],
                    AFFILIATE_NO_ELIGIBLE_PRODUCT,
                )
                self.assertEqual(row["post_intent"], "GROWTH")
                self.assertEqual(row["post_intent_epoch"], POST_INTENT_EPOCH)

    def test_hard_experiment_constraints_are_unchanged(self):
        self.assertEqual(THREADS_PUBLISHING.minimum_deal_score, 75)
        self.assertEqual(THREADS_PUBLISHING.daily_post_limit, 4)
        self.assertEqual(THREADS_PUBLISHING.cycle_post_limit, 1)
        self.assertEqual(POST_INTENT_SPLIT, "50/50")
        self.assertEqual(COMIC_MEDIA_EXPERIMENT_EPOCH, "v1")


if __name__ == "__main__":
    unittest.main()
