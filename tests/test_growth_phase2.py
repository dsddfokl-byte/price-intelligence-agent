import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.config import (
    AUTO_REPLY_MODE, COMIC_MEDIA_EXPERIMENT_EPOCH, POST_INTENT_EXPERIMENT_ENABLED,
    POST_INTENT_SPLIT, THREADS_PUBLISHING,
)
from app.database import Database
from app.growth_content import GROWTH_TEMPLATES, generate_growth_post, validate_growth_text
from app.models import Product
from app.post_intent import PostIntent, assign_post_intent
from app.publishers.threads import ThreadsCandidate, post_text_hash
from app.topic_discovery import TopicDiscovery, choose_relevant_topic, relevance_score


def candidate() -> ThreadsCandidate:
    product = Product(
        item_code="shop:1", item_name="猫砂 6L", item_price=1980,
        shop_code="shop", shop_name="shop", item_url="https://example.test/item",
        affiliate_url="https://affiliate.test/item", review_average=4.7,
        review_count=100, affiliate_rate=2.0, availability=1,
        fetched_at="2026-08-22T00:00:00+00:00",
    )
    text = "商品\nhttps://affiliate.test/item\n\n※本投稿にはアフィリエイトリンクが含まれます。"
    return ThreadsCandidate(product, 80.0, text, post_text_hash(text), "ok", "猫砂", "猫", "PRICE_CONTROL", None, None)


class FakeTopicClient:
    def __init__(self):
        self.calls = []

    def search(self, query, mode, search_type):
        self.calls.append((query, mode, search_type))
        return [{"id": "1", "text": "猫砂の話"}]


class GrowthPhase2Tests(unittest.TestCase):
    def test_deterministic_balanced_rotation(self):
        day = datetime(2026, 8, 22, tzinfo=ZoneInfo("Asia/Tokyo"))
        arms = [assign_post_intent(day.replace(hour=hour)) for hour in (7, 12, 17, 21)]
        self.assertEqual(arms.count(PostIntent.GROWTH), 2)
        self.assertEqual(arms.count(PostIntent.AFFILIATE), 2)
        self.assertEqual(arms, [assign_post_intent(day.replace(hour=hour)) for hour in (7, 12, 17, 21)])
        next_day = day + timedelta(days=1)
        # Rotation remains balanced; it is not pinned to a permanent Growth slot.
        self.assertEqual([assign_post_intent(next_day.replace(hour=h)) for h in (7, 12, 17, 21)].count(PostIntent.GROWTH), 2)

    def test_growth_has_no_commercial_content(self):
        for offset in range(12):
            post = generate_growth_post(candidate(), datetime(2026, 8, 22, tzinfo=timezone.utc) + timedelta(days=offset), "猫")
            validate_growth_text(post.text)
            self.assertIn(post.template, GROWTH_TEMPLATES)
            self.assertNotIn("https://", post.text)
            self.assertNotIn("円", post.text)
            self.assertNotIn("買い時スコア", post.text)
            self.assertNotIn("アフィリエイト", post.text)

    def test_topic_modes_cache_and_relevance(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            "CREATE TABLE topic_search_cache(cache_key TEXT PRIMARY KEY, query TEXT, search_mode TEXT, search_type TEXT, result_count INTEGER, payload_json TEXT, fetched_at TEXT);"
        )
        client = FakeTopicClient()
        discovery = TopicDiscovery(connection, client)
        now = datetime(2026, 8, 22, tzinfo=timezone.utc)
        for mode in ("KEYWORD", "TAG"):
            for search_type in ("TOP", "RECENT"):
                first = discovery.search("猫砂", mode, search_type, now)
                second = discovery.search("猫砂", mode, search_type, now)
                self.assertEqual(first.source, "API")
                self.assertEqual(second.source, "CACHE")
        self.assertEqual(len(client.calls), 4)
        self.assertEqual(choose_relevant_topic("猫砂", {"散歩": {"TOP": True, "RECENT": True}}, {}), None)
        self.assertGreaterEqual(relevance_score("猫砂", "猫砂", True, True, None), 50)

    def test_schema_and_hard_constraints(self):
        with tempfile.TemporaryDirectory() as directory:
            with Database(Path(directory) / "test.db") as database:
                database.initialize()
                columns = {row[1] for row in database.connection.execute("PRAGMA table_info(threads_posts)")}
                self.assertTrue({"post_intent", "growth_template", "unique_repliers", "conversation_depth"} <= columns)
                self.assertIsNotNone(database.connection.execute("SELECT name FROM sqlite_master WHERE name='topic_search_cache'").fetchone())
        self.assertTrue(POST_INTENT_EXPERIMENT_ENABLED)
        self.assertEqual(POST_INTENT_SPLIT, "50/50")
        self.assertEqual(AUTO_REPLY_MODE, "shadow")
        self.assertEqual(THREADS_PUBLISHING.daily_post_limit, 4)
        self.assertEqual(THREADS_PUBLISHING.cycle_post_limit, 1)
        self.assertEqual(THREADS_PUBLISHING.minimum_deal_score, 75)
        self.assertEqual(COMIC_MEDIA_EXPERIMENT_EPOCH, "v1")


if __name__ == "__main__":
    unittest.main()
