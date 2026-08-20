"""ITT, delivered, and sample-readiness reporting tests."""

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from app.database import Database
from app.init import initialize_database
from scripts.test_period_report import media_variant_rows, print_media_variant_report


class ComicMediaReportTests(unittest.TestCase):
    def test_assigned_and_delivered_are_reported_separately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "report.db")
            initialize_database(database)
            database.connection.execute(
                """
                INSERT INTO products(item_code, first_seen_at, last_seen_at)
                VALUES ('shop:item', '2026-08-01T00:00:00+00:00',
                        '2026-08-01T00:00:00+00:00')
                """
            )
            for index, delivered in enumerate(("COMIC", "NO_COMIC"), 1):
                database.record_threads_post(
                    item_code="shop:item", threads_post_id=f"post-{index}",
                    posted_at=f"2026-08-2{index}T00:00:00+00:00",
                    deal_score=80, price=1000, text_hash=f"hash-{index}",
                    status="published", assigned_media_variant="COMIC",
                    delivered_media_variant=delivered,
                    comic_fallback_reason=(
                        "COMIC_MEDIA_FAILED" if delivered == "NO_COMIC" else None
                    ),
                )
            database.connection.execute(
                "UPDATE threads_posts SET views=100, likes=5, replies=2, reposts=1"
            )
            itt = media_variant_rows(
                database.connection, "2026-08-01T00:00:00+00:00",
                "assigned_media_variant",
            )
            delivered = media_variant_rows(
                database.connection, "2026-08-01T00:00:00+00:00",
                "delivered_media_variant",
            )
            self.assertEqual([(row["media_variant"], row["published_count"]) for row in itt], [("COMIC", 2)])
            self.assertEqual({row["media_variant"]: row["published_count"] for row in delivered}, {"COMIC": 1, "NO_COMIC": 1})
            output = io.StringIO()
            with redirect_stdout(output):
                print_media_variant_report(
                    database.connection, "2026-08-01T00:00:00+00:00"
                )
            report = output.getvalue()
            self.assertIn("ITT（assigned基準）", report)
            self.assertIn("Delivered（配信実績基準）", report)
            self.assertIn("INSUFFICIENT_SAMPLE", report)
            self.assertIn("fallback=1", report)
            database.close()

    def test_invalid_report_dimension_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "report.db")
            initialize_database(database)
            with self.assertRaises(ValueError):
                media_variant_rows(database.connection, "2026-01-01", "unsafe")
            database.close()


if __name__ == "__main__":
    unittest.main()
