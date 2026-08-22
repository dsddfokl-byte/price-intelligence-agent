import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import (
    COMIC_MEDIA_EXPERIMENT_EPOCH,
    POST_INTENT_SPLIT,
    THREADS_PUBLISHING,
    THREADS_TOPIC_TAGS,
)
from app.database import Database, SCHEMA
from app.growth_content import generate_generic_growth_post, validate_growth_text
from app.post_intent import PostIntent, assign_post_intent


class GrowthCandidateDecouplingTests(unittest.TestCase):
    def test_growth_is_generated_without_affiliate_candidates_or_deal_score(self):
        now = datetime(2026, 8, 22, 12, 30, tzinfo=ZoneInfo("Asia/Tokyo"))
        post = generate_generic_growth_post(now, tuple(THREADS_TOPIC_TAGS), THREADS_TOPIC_TAGS)
        validate_growth_text(post.text)
        self.assertEqual(post.post_intent, "GROWTH")
        self.assertEqual(post.deal_score, 0.0)
        self.assertIsNone(post.product.affiliate_url)
        self.assertIsNone(post.product.item_price)
        self.assertTrue(post.product.item_code.startswith("growth:"))

    def test_four_slots_are_balanced_and_growth_needs_no_product_pool(self):
        base = datetime(2026, 8, 22, tzinfo=ZoneInfo("Asia/Tokyo"))
        intents = []
        for hour in (7, 12, 17, 21):
            now = base.replace(hour=hour, minute=30)
            intent = assign_post_intent(now)
            intents.append(intent)
            if intent == PostIntent.GROWTH:
                post = generate_generic_growth_post(
                    now, tuple(THREADS_TOPIC_TAGS), THREADS_TOPIC_TAGS
                )
                validate_growth_text(post.text)
        self.assertEqual(intents.count(PostIntent.GROWTH), 2)
        self.assertEqual(intents.count(PostIntent.AFFILIATE), 2)

    def test_threads_posts_migration_preserves_rows_and_allows_null_item(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            legacy_schema = SCHEMA.replace(
                "CREATE TABLE IF NOT EXISTS threads_posts (\n"
                "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                "    item_code TEXT,",
                "CREATE TABLE IF NOT EXISTS threads_posts (\n"
                "    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
                "    item_code TEXT NOT NULL,",
            )
            connection.executescript(legacy_schema)
            connection.execute(
                """INSERT INTO products(
                       item_code, first_seen_at, last_seen_at
                   ) VALUES ('shop:1', '2026-08-22', '2026-08-22')"""
            )
            connection.execute(
                """INSERT INTO threads_posts(
                       item_code, posted_at, deal_score, text_hash, status
                   ) VALUES ('shop:1', '2026-08-22T00:00:00+00:00', 80, 'hash', 'published')"""
            )
            connection.commit()
            connection.close()
            with Database(path) as database:
                database.initialize()
                info = {
                    row["name"]: row for row in database.connection.execute(
                        "PRAGMA table_info(threads_posts)"
                    )
                }
                self.assertEqual(info["item_code"]["notnull"], 0)
                self.assertEqual(
                    database.connection.execute(
                        "SELECT COUNT(*) FROM threads_posts"
                    ).fetchone()[0],
                    1,
                )
                database.record_threads_post(
                    item_code=None,
                    threads_post_id="growth-post",
                    posted_at="2026-08-22T01:00:00+00:00",
                    deal_score=0,
                    price=None,
                    text_hash="growth-hash",
                    status="published",
                    post_intent="GROWTH",
                )

    def test_hard_constraints_unchanged(self):
        self.assertEqual(THREADS_PUBLISHING.minimum_deal_score, 75)
        self.assertEqual(THREADS_PUBLISHING.daily_post_limit, 4)
        self.assertEqual(THREADS_PUBLISHING.cycle_post_limit, 1)
        self.assertEqual(POST_INTENT_SPLIT, "50/50")
        self.assertEqual(COMIC_MEDIA_EXPERIMENT_EPOCH, "v1")


if __name__ == "__main__":
    unittest.main()
