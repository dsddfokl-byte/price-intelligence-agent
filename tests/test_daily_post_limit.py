"""Published-only daily and per-cycle post-limit regression tests."""

import tempfile
import unittest
from pathlib import Path

from app.config import THREADS_PUBLISHING
from app.database import Database
from app.init import initialize_database


SINCE = "2026-08-21T00:00:00+00:00"


class DailyPostLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "limit.db")
        initialize_database(self.database)
        self.database.connection.execute(
            """
            INSERT INTO products(item_code, first_seen_at, last_seen_at)
            VALUES ('shop:item', ?, ?)
            """,
            (SINCE, SINCE),
        )

    def tearDown(self) -> None:
        self.database.close()
        self.temporary.cleanup()

    def record(self, index: int, status: str, assigned: str, delivered: str) -> None:
        self.database.record_threads_post(
            item_code="shop:item",
            threads_post_id=f"post-{index}" if status == "published" else None,
            posted_at=f"2026-08-21T0{index}:00:00+00:00",
            deal_score=75.0,
            price=1000,
            text_hash=f"hash-{index}",
            status=status,
            assigned_media_variant=assigned,
            delivered_media_variant=delivered,
        )

    def test_zero_through_three_allow_next_post_and_four_blocks(self) -> None:
        self.assertEqual(THREADS_PUBLISHING.daily_post_limit, 4)
        self.assertEqual(THREADS_PUBLISHING.cycle_post_limit, 1)
        for published_count in range(5):
            actual = self.database.published_threads_count_since(SINCE)
            self.assertEqual(actual, published_count)
            self.assertEqual(
                actual < THREADS_PUBLISHING.daily_post_limit,
                published_count in (0, 1, 2, 3),
            )
            if published_count < 4:
                variant = "COMIC" if published_count % 2 == 0 else "NO_COMIC"
                self.record(published_count, "published", variant, variant)

    def test_failed_publish_is_not_counted(self) -> None:
        self.record(1, "failed", "COMIC", "NO_COMIC")
        self.assertEqual(self.database.published_threads_count_since(SINCE), 0)

    def test_comic_text_and_fallback_each_count_once(self) -> None:
        self.record(1, "published", "COMIC", "COMIC")
        self.record(2, "published", "NO_COMIC", "NO_COMIC")
        self.record(3, "published", "COMIC", "NO_COMIC")
        self.assertEqual(self.database.published_threads_count_since(SINCE), 3)


if __name__ == "__main__":
    unittest.main()
