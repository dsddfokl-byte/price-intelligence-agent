"""Single-publish safety and atomic comic persistence tests."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from app.comics.media_publisher import ComicThreadsPublisher
from app.config import (
    COMIC_PRODUCTION_TEST_LIMIT,
    COMIC_STOCK_PUBLISHING_ENABLED,
    THREADS_PUBLISH_CALL_LIMIT,
)
from app.database import Database
from app.init import initialize_database
from app.publishers.threads import ThreadsPostError


class ComicProductionPublishTests(unittest.TestCase):
    def test_publish_is_single_call_and_requires_finished_container(self) -> None:
        publisher = ComicThreadsPublisher("test-token")
        publisher.get_user_id = Mock(return_value="user-id")
        response = Mock(status_code=200)
        response.json.return_value = {"id": "post-id"}
        publisher.session.request = Mock(return_value=response)
        self.assertEqual(
            publisher.publish_finished_container_once("creation-id", "FINISHED"),
            "post-id",
        )
        publisher.session.request.assert_called_once()
        with self.assertRaises(ThreadsPostError):
            publisher.publish_finished_container_once("creation-id", "IN_PROGRESS")
        publisher.session.request.assert_called_once()
        publisher.close()
        self.assertEqual(COMIC_PRODUCTION_TEST_LIMIT, 1)
        self.assertEqual(THREADS_PUBLISH_CALL_LIMIT, 1)
        self.assertFalse(COMIC_STOCK_PUBLISHING_ENABLED)

    def test_success_records_post_and_usage_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db")
            initialize_database(database)
            database.connection.execute(
                """
                INSERT INTO products(item_code, first_seen_at, last_seen_at)
                VALUES ('shop:item', '2026-08-21T00:00:00+00:00',
                        '2026-08-21T00:00:00+00:00')
                """
            )
            database.record_published_comic_post(
                item_code="shop:item", threads_post_id="post-id",
                posted_at="2026-08-21T00:00:00+00:00", deal_score=80.0,
                price=1000, text_hash="hash", topic_tag="猫",
                template_variant="PRICE_CONTROL", tip_id=None,
                content_trigger=None, search_keyword="猫砂",
                comic_id="comic_001", comic_file="source.png",
                comic_stock_version="comic_stock_v1",
                media_url="https://example.com/comic_001.png",
                media_hosting_provider="github_pages",
                selected_at="2026-08-21T00:00:00+00:00",
                selection_score=150, selection_reason="category=100",
            )
            post = database.connection.execute("SELECT * FROM threads_posts").fetchone()
            usage = database.connection.execute("SELECT * FROM comic_usage").fetchone()
            self.assertEqual(post["status"], "published")
            self.assertEqual(post["delivered_media_variant"], "COMIC")
            self.assertEqual(usage["delivered"], "COMIC")
            self.assertEqual(usage["thread_post_id"], "post-id")
            database.close()

    def test_failure_before_success_write_changes_no_counters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db")
            initialize_database(database)
            before = (
                database.connection.execute("SELECT COUNT(*) FROM threads_posts").fetchone()[0],
                database.connection.execute("SELECT COUNT(*) FROM comic_usage").fetchone()[0],
            )
            publisher = ComicThreadsPublisher("test-token")
            publisher.get_user_id = Mock(return_value="user-id")
            with self.assertRaises(ThreadsPostError):
                publisher.publish_finished_container_once("creation-id", "ERROR")
            publisher.close()
            after = (
                database.connection.execute("SELECT COUNT(*) FROM threads_posts").fetchone()[0],
                database.connection.execute("SELECT COUNT(*) FROM comic_usage").fetchone()[0],
            )
            self.assertEqual(before, after)
            database.close()


if __name__ == "__main__":
    unittest.main()
