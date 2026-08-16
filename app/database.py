"""SQLite persistence for products, price history, and collections."""

import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from app.models import Product


SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    item_code TEXT PRIMARY KEY,
    item_name TEXT,
    latest_price INTEGER,
    shop_code TEXT,
    shop_name TEXT,
    item_url TEXT,
    affiliate_url TEXT,
    review_average REAL,
    review_count INTEGER,
    affiliate_rate REAL,
    availability INTEGER,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_code TEXT NOT NULL,
    price INTEGER,
    fetched_at TEXT NOT NULL,
    UNIQUE(item_code, fetched_at),
    FOREIGN KEY(item_code) REFERENCES products(item_code)
);

CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    item_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS threads_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    item_code TEXT NOT NULL,
    threads_post_id TEXT,
    posted_at TEXT NOT NULL,
    deal_score REAL NOT NULL,
    price INTEGER,
    text_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    FOREIGN KEY(item_code) REFERENCES products(item_code)
);

CREATE INDEX IF NOT EXISTS idx_threads_posts_item_code
    ON threads_posts(item_code);
CREATE INDEX IF NOT EXISTS idx_threads_posts_posted_at
    ON threads_posts(posted_at);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def initialize(self) -> None:
        with self.connection:
            self.connection.executescript(SCHEMA)

    def start_collection(self, keyword: str, started_at: str) -> int:
        with self.connection:
            cursor = self.connection.execute(
                "INSERT INTO collections(keyword, started_at, status) VALUES (?, ?, ?)",
                (keyword, started_at, "running"),
            )
        return int(cursor.lastrowid)

    def finish_collection(
        self,
        collection_id: int,
        finished_at: str,
        item_count: int,
        status: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE collections
                SET finished_at = ?, item_count = ?, status = ?
                WHERE id = ?
                """,
                (finished_at, item_count, status, collection_id),
            )

    def previous_price(self, item_code: str) -> Optional[int]:
        row = self.connection.execute(
            """
            SELECT price FROM price_history
            WHERE item_code = ? AND price IS NOT NULL
            ORDER BY fetched_at DESC LIMIT 1
            """,
            (item_code,),
        ).fetchone()
        return int(row["price"]) if row is not None else None

    def price_history_count(self, item_code: str) -> int:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM price_history
            WHERE item_code = ? AND price IS NOT NULL
            """,
            (item_code,),
        ).fetchone()
        return int(row["count"])

    def save_products(self, products: Iterable[Product]) -> None:
        with self.connection:
            for product in products:
                self.connection.execute(
                    """
                    INSERT INTO products (
                        item_code, item_name, latest_price, shop_code, shop_name,
                        item_url, affiliate_url, review_average, review_count,
                        affiliate_rate, availability, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(item_code) DO UPDATE SET
                        item_name = excluded.item_name,
                        latest_price = excluded.latest_price,
                        shop_code = excluded.shop_code,
                        shop_name = excluded.shop_name,
                        item_url = excluded.item_url,
                        affiliate_url = excluded.affiliate_url,
                        review_average = excluded.review_average,
                        review_count = excluded.review_count,
                        affiliate_rate = excluded.affiliate_rate,
                        availability = excluded.availability,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        product.item_code,
                        product.item_name,
                        product.item_price,
                        product.shop_code,
                        product.shop_name,
                        product.item_url,
                        product.affiliate_url,
                        product.review_average,
                        product.review_count,
                        product.affiliate_rate,
                        product.availability,
                        product.fetched_at,
                        product.fetched_at,
                    ),
                )
                self.connection.execute(
                    """
                    INSERT OR IGNORE INTO price_history(item_code, price, fetched_at)
                    VALUES (?, ?, ?)
                    """,
                    (product.item_code, product.item_price, product.fetched_at),
                )

    def products_for_threads(self) -> Iterable[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT p.*,
                   (
                       SELECT COUNT(*) FROM price_history ph
                       WHERE ph.item_code = p.item_code AND ph.price IS NOT NULL
                   ) AS price_history_count,
                   (
                       SELECT ph.price FROM price_history ph
                       WHERE ph.item_code = p.item_code
                         AND ph.price IS NOT NULL
                       ORDER BY ph.fetched_at DESC
                       LIMIT 1 OFFSET 1
                   ) AS previous_price
            FROM products p
            ORDER BY p.last_seen_at DESC
            """
        ).fetchall()

    def product_for_threads(self, item_code: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT p.*,
                   (
                       SELECT COUNT(*) FROM price_history ph
                       WHERE ph.item_code = p.item_code AND ph.price IS NOT NULL
                   ) AS price_history_count,
                   (
                       SELECT ph.price FROM price_history ph
                       WHERE ph.item_code = p.item_code
                         AND ph.price IS NOT NULL
                       ORDER BY ph.fetched_at DESC
                       LIMIT 1 OFFSET 1
                   ) AS previous_price
            FROM products p
            WHERE p.item_code = ?
            """,
            (item_code,),
        ).fetchone()

    def latest_published_threads_post(self, item_code: str) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT * FROM threads_posts
            WHERE item_code = ? AND status = 'published'
            ORDER BY posted_at DESC LIMIT 1
            """,
            (item_code,),
        ).fetchone()

    def has_published_text_hash_since(self, text_hash: str, since: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM threads_posts
            WHERE text_hash = ? AND status = 'published' AND posted_at >= ?
            LIMIT 1
            """,
            (text_hash, since),
        ).fetchone()
        return row is not None

    def published_threads_count_since(self, since: str) -> int:
        row = self.connection.execute(
            """
            SELECT COUNT(*) AS count FROM threads_posts
            WHERE status = 'published' AND posted_at >= ?
            """,
            (since,),
        ).fetchone()
        return int(row["count"])

    def record_threads_post(
        self,
        item_code: str,
        threads_post_id: Optional[str],
        posted_at: str,
        deal_score: float,
        price: Optional[int],
        text_hash: str,
        status: str,
        error: Optional[str] = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO threads_posts(
                    item_code, threads_post_id, posted_at, deal_score,
                    price, text_hash, status, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item_code,
                    threads_post_id,
                    posted_at,
                    deal_score,
                    price,
                    text_hash,
                    status,
                    error,
                ),
            )
