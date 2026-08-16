"""Regression tests for collection-cycle persistence."""

import logging
import tempfile
import unittest
from pathlib import Path

from app.collector import collect_products
from app.database import Database
from app.models import Product


def product(fetched_at: str) -> Product:
    return Product(
        item_code="shop:shared-item",
        item_name="shared product",
        item_price=1980,
        shop_code="shop",
        shop_name="shop",
        item_url="https://example.invalid/item",
        affiliate_url="https://example.invalid/affiliate-item",
        review_average=4.5,
        review_count=100,
        affiliate_rate=4.0,
        availability=1,
        fetched_at=fetched_at,
    )


class StubClient:
    def search(self, keyword: str, hits: int = 30):
        fetched_at = {
            "keyword-a": "2026-08-16T10:00:00+00:00",
            "keyword-b": "2026-08-16T10:00:01+00:00",
        }[keyword]
        return [product(fetched_at)]


class CollectorPersistenceTests(unittest.TestCase):
    def test_shared_product_has_one_history_observation_per_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "test.db")
            database.initialize()

            result = collect_products(
                database,
                StubClient(),
                ["keyword-a", "keyword-b"],
                logging.getLogger("test"),
            )

            self.assertEqual(result.fetched_count, 2)
            self.assertEqual(result.updated_count, 1)
            self.assertEqual(
                database.connection.execute(
                    "SELECT COUNT(*) FROM price_history WHERE item_code = ?",
                    ("shop:shared-item",),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                database.connection.execute(
                    "SELECT COUNT(*) FROM product_keywords WHERE item_code = ?",
                    ("shop:shared-item",),
                ).fetchone()[0],
                2,
            )
            database.close()


if __name__ == "__main__":
    unittest.main()
