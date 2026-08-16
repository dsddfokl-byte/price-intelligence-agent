"""SQLite persistence for products, price history, and collections."""

import sqlite3
from pathlib import Path
from typing import AbstractSet, Iterable, Optional

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
    point_rate INTEGER,
    point_rate_start_time TEXT,
    point_rate_end_time TEXT,
    postage_flag INTEGER,
    sale_start_time TEXT,
    sale_end_time TEXT,
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
    views INTEGER,
    likes INTEGER,
    replies INTEGER,
    reposts INTEGER,
    quotes INTEGER,
    shares INTEGER,
    insights_updated_at TEXT,
    topic_tag TEXT,
    template_variant TEXT,
    tip_id TEXT,
    content_trigger TEXT,
    search_keyword TEXT,
    FOREIGN KEY(item_code) REFERENCES products(item_code)
);

CREATE TABLE IF NOT EXISTS product_keywords (
    item_code TEXT NOT NULL,
    keyword TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY(item_code, keyword),
    FOREIGN KEY(item_code) REFERENCES products(item_code)
);

CREATE INDEX IF NOT EXISTS idx_threads_posts_item_code
    ON threads_posts(item_code);
CREATE INDEX IF NOT EXISTS idx_threads_posts_posted_at
    ON threads_posts(posted_at);
CREATE INDEX IF NOT EXISTS idx_product_keywords_keyword
    ON product_keywords(keyword);
"""

PRODUCT_MIGRATION_COLUMNS = {
    "point_rate": "INTEGER",
    "point_rate_start_time": "TEXT",
    "point_rate_end_time": "TEXT",
    "postage_flag": "INTEGER",
    "sale_start_time": "TEXT",
    "sale_end_time": "TEXT",
}

THREADS_INSIGHTS_MIGRATION_COLUMNS = {
    "views": "INTEGER",
    "likes": "INTEGER",
    "replies": "INTEGER",
    "reposts": "INTEGER",
    "quotes": "INTEGER",
    "shares": "INTEGER",
    "insights_updated_at": "TEXT",
    "topic_tag": "TEXT",
    "template_variant": "TEXT",
    "tip_id": "TEXT",
    "content_trigger": "TEXT",
    "search_keyword": "TEXT",
}


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
            self._ensure_columns("products", PRODUCT_MIGRATION_COLUMNS)
            self._ensure_columns("threads_posts", THREADS_INSIGHTS_MIGRATION_COLUMNS)

    def _ensure_columns(self, table: str, columns: dict) -> None:
        existing = {
            row["name"]
            for row in self.connection.execute(f"PRAGMA table_info({table})")
        }
        for name, declaration in columns.items():
            if name not in existing:
                self.connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                )

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

    def save_products(
        self,
        products: Iterable[Product],
        *,
        history_item_codes: Optional[AbstractSet[str]] = None,
    ) -> None:
        with self.connection:
            for product in products:
                self.connection.execute(
                    """
                    INSERT INTO products (
                        item_code, item_name, latest_price, shop_code, shop_name,
                        item_url, affiliate_url, review_average, review_count,
                        affiliate_rate, availability, point_rate,
                        point_rate_start_time, point_rate_end_time, postage_flag,
                        sale_start_time, sale_end_time, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        point_rate = excluded.point_rate,
                        point_rate_start_time = excluded.point_rate_start_time,
                        point_rate_end_time = excluded.point_rate_end_time,
                        postage_flag = excluded.postage_flag,
                        sale_start_time = excluded.sale_start_time,
                        sale_end_time = excluded.sale_end_time,
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
                        product.point_rate,
                        product.point_rate_start_time,
                        product.point_rate_end_time,
                        product.postage_flag,
                        product.sale_start_time,
                        product.sale_end_time,
                        product.fetched_at,
                        product.fetched_at,
                    ),
                )
                if history_item_codes is None or product.item_code in history_item_codes:
                    self.connection.execute(
                        """
                        INSERT OR IGNORE INTO price_history(item_code, price, fetched_at)
                        VALUES (?, ?, ?)
                        """,
                        (product.item_code, product.item_price, product.fetched_at),
                    )

    def save_product_keywords(
        self, keyword: str, item_codes: Iterable[str], seen_at: str
    ) -> None:
        with self.connection:
            self.connection.executemany(
                """
                INSERT INTO product_keywords(item_code, keyword, last_seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(item_code, keyword) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at
                """,
                ((item_code, keyword, seen_at) for item_code in item_codes),
            )

    def published_threads_posts_since(self, since: str) -> Iterable[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT * FROM threads_posts
            WHERE status = 'published'
              AND threads_post_id IS NOT NULL
              AND posted_at >= ?
            ORDER BY posted_at DESC
            """,
            (since,),
        ).fetchall()

    def update_threads_insights(
        self,
        threads_post_id: str,
        *,
        views: Optional[int],
        likes: Optional[int],
        replies: Optional[int],
        reposts: Optional[int],
        quotes: Optional[int],
        shares: Optional[int],
        updated_at: str,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                UPDATE threads_posts SET
                    views = ?, likes = ?, replies = ?, reposts = ?,
                    quotes = ?, shares = ?, insights_updated_at = ?
                WHERE threads_post_id = ? AND status = 'published'
                """,
                (
                    views, likes, replies, reposts, quotes, shares,
                    updated_at, threads_post_id,
                ),
            )

    def products_for_threads(self) -> Iterable[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT p.*,
                   (
                       SELECT pk.keyword FROM product_keywords pk
                       WHERE pk.item_code = p.item_code
                       ORDER BY pk.last_seen_at DESC, pk.keyword
                       LIMIT 1
                   ) AS search_keyword,
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
                       SELECT pk.keyword FROM product_keywords pk
                       WHERE pk.item_code = p.item_code
                       ORDER BY pk.last_seen_at DESC, pk.keyword
                       LIMIT 1
                   ) AS search_keyword,
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

    def has_published_tip_since(self, tip_id: str, since: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM threads_posts
            WHERE tip_id = ? AND status = 'published' AND posted_at >= ?
            LIMIT 1
            """,
            (tip_id, since),
        ).fetchone()
        return row is not None

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
        topic_tag: Optional[str] = None,
        template_variant: Optional[str] = None,
        tip_id: Optional[str] = None,
        content_trigger: Optional[str] = None,
        search_keyword: Optional[str] = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO threads_posts(
                    item_code, threads_post_id, posted_at, deal_score,
                    price, text_hash, status, error, topic_tag,
                    template_variant, tip_id, content_trigger, search_keyword
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    topic_tag,
                    template_variant,
                    tip_id,
                    content_trigger,
                    search_keyword,
                ),
            )
