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
    experiment_arm TEXT,
    assignment_key TEXT,
    experiment_epoch TEXT,
    clicks INTEGER,
    pending_orders INTEGER,
    pending_commission REAL,
    confirmed_orders INTEGER,
    confirmed_commission REAL,
    outcome_status TEXT,
    comic_id TEXT,
    comic_file TEXT,
    comic_stock_version TEXT,
    assigned_media_variant TEXT,
    delivered_media_variant TEXT,
    media_url TEXT,
    media_url_expires_at TEXT,
    media_hosting_provider TEXT,
    comic_media_experiment_epoch TEXT,
    comic_fallback_reason TEXT,
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

CREATE TABLE IF NOT EXISTS autopilot_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    execution_state TEXT NOT NULL,
    reward_mode TEXT NOT NULL,
    experiment_epoch TEXT NOT NULL,
    optimizer_model_version TEXT NOT NULL,
    controller_version TEXT NOT NULL,
    scoring_formula_version TEXT NOT NULL,
    experiment_policy_version TEXT NOT NULL,
    revenue_data_ready INTEGER NOT NULL DEFAULT 0,
    safe_halt_reason TEXT,
    safe_halt_class TEXT,
    manual_clear_required INTEGER NOT NULL DEFAULT 0,
    health_success_count INTEGER NOT NULL DEFAULT 0,
    healthy_since TEXT,
    promotion_success_count INTEGER NOT NULL DEFAULT 0,
    last_promotion_eval_at TEXT,
    state_entered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL UNIQUE,
    experiment_epoch TEXT NOT NULL,
    experiment_arm TEXT NOT NULL,
    assignment_key TEXT NOT NULL,
    reward_mode TEXT NOT NULL,
    assigned_at TEXT NOT NULL,
    selector_used TEXT,
    selected_item_code TEXT,
    candidate_score REAL,
    decision TEXT,
    publish_attempted INTEGER NOT NULL DEFAULT 0,
    publish_status TEXT,
    is_formal_experiment INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS controller_evaluations (
    controller_evaluation_id TEXT PRIMARY KEY,
    evaluated_at TEXT NOT NULL,
    result_execution_state TEXT NOT NULL,
    result_reward_mode TEXT NOT NULL,
    experiment_epoch TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS autopilot_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    controller_evaluation_id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    from_execution_state TEXT NOT NULL,
    to_execution_state TEXT NOT NULL,
    from_reward_mode TEXT NOT NULL,
    to_reward_mode TEXT NOT NULL,
    experiment_epoch TEXT NOT NULL,
    reason TEXT NOT NULL,
    FOREIGN KEY(controller_evaluation_id)
        REFERENCES controller_evaluations(controller_evaluation_id)
);

INSERT OR IGNORE INTO autopilot_state (
    id, execution_state, reward_mode, experiment_epoch,
    optimizer_model_version, controller_version, scoring_formula_version,
    experiment_policy_version, revenue_data_ready, state_entered_at, updated_at
) VALUES (
    1, 'SHADOW', 'ENGAGEMENT_PROXY', 'epoch-initial-v1',
    'optimizer-v1', 'controller-v1', 'scoring-v1',
    'policy-v1', 0, datetime('now'), datetime('now')
);

CREATE INDEX IF NOT EXISTS idx_experiment_cycles_epoch_arm
    ON experiment_cycles(experiment_epoch, experiment_arm);

CREATE TABLE IF NOT EXISTS post_performance_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    threads_post_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    horizon_hours REAL NOT NULL,
    is_valid_72h INTEGER NOT NULL DEFAULT 0,
    views INTEGER,
    likes INTEGER,
    replies INTEGER,
    reposts INTEGER,
    quotes INTEGER,
    shares INTEGER,
    UNIQUE(threads_post_id, captured_at)
);

CREATE TABLE IF NOT EXISTS optimizer_shadow_candidate_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL,
    run_mode TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    training_data_cutoff TEXT NOT NULL,
    item_code TEXT NOT NULL,
    category TEXT,
    template_variant TEXT,
    candidate_source TEXT NOT NULL,
    base_quality_score REAL NOT NULL,
    global_mean_proxy REAL,
    category_n INTEGER NOT NULL,
    category_smoothed_proxy REAL,
    category_delta REAL,
    template_n INTEGER NOT NULL,
    template_smoothed_proxy REAL,
    template_delta REAL,
    historical_adjustment REAL NOT NULL,
    optimizer_score REAL NOT NULL,
    reward_mode TEXT NOT NULL,
    model_version TEXT NOT NULL,
    experiment_epoch TEXT NOT NULL,
    rank_position INTEGER NOT NULL,
    UNIQUE(cycle_id, item_code)
);

CREATE TABLE IF NOT EXISTS optimizer_shadow_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle_id TEXT NOT NULL UNIQUE,
    run_mode TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    training_data_cutoff TEXT NOT NULL,
    production_item_code TEXT,
    optimizer_item_code TEXT,
    selected_item_code TEXT,
    selected_base_score REAL,
    selected_historical_adjustment REAL,
    selected_optimizer_score REAL,
    category_n INTEGER,
    template_n INTEGER,
    category_contribution REAL,
    template_contribution REAL,
    reason_summary TEXT NOT NULL,
    reward_mode TEXT NOT NULL,
    model_version TEXT NOT NULL,
    experiment_epoch TEXT NOT NULL,
    experiment_arm TEXT
);

CREATE INDEX IF NOT EXISTS idx_performance_snapshots_cutoff
    ON post_performance_snapshots(is_valid_72h, captured_at);
CREATE INDEX IF NOT EXISTS idx_optimizer_scores_cycle_rank
    ON optimizer_shadow_candidate_scores(cycle_id, rank_position);

CREATE TABLE IF NOT EXISTS comic_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comic_id TEXT NOT NULL,
    thread_post_id TEXT,
    item_code TEXT NOT NULL,
    category TEXT NOT NULL,
    selected_at TEXT NOT NULL,
    published_at TEXT,
    assigned TEXT NOT NULL,
    delivered TEXT NOT NULL,
    selection_score REAL NOT NULL,
    selection_reason TEXT NOT NULL,
    comic_media_experiment_epoch TEXT
);

CREATE INDEX IF NOT EXISTS idx_comic_usage_comic_selected
    ON comic_usage(comic_id, selected_at);
CREATE INDEX IF NOT EXISTS idx_comic_usage_item
    ON comic_usage(item_code, comic_id);

CREATE TABLE IF NOT EXISTS growth_experiments (
    experiment_id TEXT PRIMARY KEY,
    experiment_family TEXT NOT NULL,
    epoch TEXT NOT NULL,
    status TEXT NOT NULL,
    control_arm TEXT NOT NULL,
    treatment_arm TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT,
    primary_metric TEXT NOT NULL,
    minimum_samples_per_arm INTEGER NOT NULL,
    minimum_runtime_days INTEGER NOT NULL,
    decision TEXT,
    decision_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_growth_experiments_status
    ON growth_experiments(status, created_at);
"""

EXPERIMENT_CYCLES_MIGRATION_COLUMNS = {
    "candidate_score": "REAL",
    "decision": "TEXT",
}

COMIC_USAGE_MIGRATION_COLUMNS = {
    "comic_media_experiment_epoch": "TEXT",
}

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
    "experiment_arm": "TEXT",
    "assignment_key": "TEXT",
    "experiment_epoch": "TEXT",
    "clicks": "INTEGER",
    "pending_orders": "INTEGER",
    "pending_commission": "REAL",
    "confirmed_orders": "INTEGER",
    "confirmed_commission": "REAL",
    "outcome_status": "TEXT",
    "comic_id": "TEXT",
    "comic_file": "TEXT",
    "comic_stock_version": "TEXT",
    "assigned_media_variant": "TEXT",
    "delivered_media_variant": "TEXT",
    "media_url": "TEXT",
    "media_url_expires_at": "TEXT",
    "media_hosting_provider": "TEXT",
    "comic_media_experiment_epoch": "TEXT",
    "comic_fallback_reason": "TEXT",
}

AUTOPILOT_STATE_MIGRATION_COLUMNS = {
    "promotion_success_count": "INTEGER NOT NULL DEFAULT 0",
    "last_promotion_eval_at": "TEXT",
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
            self._ensure_columns("comic_usage", COMIC_USAGE_MIGRATION_COLUMNS)
            self._ensure_columns("autopilot_state", AUTOPILOT_STATE_MIGRATION_COLUMNS)
            self._ensure_columns("experiment_cycles", EXPERIMENT_CYCLES_MIGRATION_COLUMNS)

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
            self.connection.execute(
                """
                INSERT OR IGNORE INTO post_performance_snapshots(
                    threads_post_id, captured_at, horizon_hours, is_valid_72h,
                    views, likes, replies, reposts, quotes, shares
                )
                SELECT threads_post_id, ?,
                       (julianday(?) - julianday(posted_at)) * 24.0,
                       CASE WHEN (julianday(?) - julianday(posted_at)) * 24.0
                                      BETWEEN 72.0 AND 96.0
                            THEN 1 ELSE 0 END,
                       ?, ?, ?, ?, ?, ?
                FROM threads_posts
                WHERE threads_post_id = ? AND status = 'published'
                """,
                (
                    updated_at, updated_at, updated_at,
                    views, likes, replies, reposts, quotes, shares,
                    threads_post_id,
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
        experiment_arm: Optional[str] = None,
        assignment_key: Optional[str] = None,
        experiment_epoch: Optional[str] = None,
        comic_id: Optional[str] = None,
        comic_file: Optional[str] = None,
        comic_stock_version: Optional[str] = None,
        assigned_media_variant: Optional[str] = None,
        delivered_media_variant: Optional[str] = None,
        media_url: Optional[str] = None,
        media_url_expires_at: Optional[str] = None,
        media_hosting_provider: Optional[str] = None,
        comic_media_experiment_epoch: Optional[str] = None,
        comic_fallback_reason: Optional[str] = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO threads_posts(
                    item_code, threads_post_id, posted_at, deal_score,
                    price, text_hash, status, error, topic_tag,
                    template_variant, tip_id, content_trigger, search_keyword,
                    experiment_arm, assignment_key, experiment_epoch,
                    comic_id, comic_file, comic_stock_version,
                    assigned_media_variant, delivered_media_variant,
                    media_url, media_url_expires_at, media_hosting_provider,
                    comic_media_experiment_epoch, comic_fallback_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    experiment_arm,
                    assignment_key,
                    experiment_epoch,
                    comic_id,
                    comic_file,
                    comic_stock_version,
                    assigned_media_variant,
                    delivered_media_variant,
                    media_url,
                    media_url_expires_at,
                    media_hosting_provider,
                    comic_media_experiment_epoch,
                    comic_fallback_reason,
                ),
            )

    def comic_usage_rows(self) -> Iterable[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM comic_usage ORDER BY selected_at DESC"
        ).fetchall()

    def record_comic_usage(
        self,
        *,
        comic_id: str,
        thread_post_id: Optional[str],
        item_code: str,
        category: str,
        selected_at: str,
        published_at: Optional[str],
        assigned: str,
        delivered: str,
        selection_score: float,
        selection_reason: str,
        comic_media_experiment_epoch: Optional[str] = None,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO comic_usage(
                    comic_id, thread_post_id, item_code, category, selected_at,
                    published_at, assigned, delivered, selection_score,
                    selection_reason, comic_media_experiment_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comic_id, thread_post_id, item_code, category, selected_at,
                    published_at, assigned, delivered, selection_score,
                    selection_reason,
                    comic_media_experiment_epoch,
                ),
            )

    def record_published_comic_post(
        self,
        *,
        item_code: str,
        threads_post_id: str,
        posted_at: str,
        deal_score: float,
        price: Optional[int],
        text_hash: str,
        topic_tag: str,
        template_variant: str,
        tip_id: Optional[str],
        content_trigger: Optional[str],
        search_keyword: str,
        comic_id: str,
        comic_file: str,
        comic_stock_version: str,
        media_url: str,
        media_hosting_provider: str,
        selected_at: str,
        selection_score: float,
        selection_reason: str,
        experiment_arm: Optional[str] = None,
        assignment_key: Optional[str] = None,
        experiment_epoch: Optional[str] = None,
        comic_media_experiment_epoch: Optional[str] = None,
    ) -> None:
        """Atomically persist one successfully published comic post and usage."""
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO threads_posts(
                    item_code, threads_post_id, posted_at, deal_score,
                    price, text_hash, status, topic_tag, template_variant,
                    tip_id, content_trigger, search_keyword,
                    experiment_arm, assignment_key, experiment_epoch,
                    comic_id, comic_file, comic_stock_version,
                    assigned_media_variant, delivered_media_variant,
                    media_url, media_hosting_provider,
                    comic_media_experiment_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, 'published', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          'COMIC', 'COMIC', ?, ?, ?)
                """,
                (
                    item_code, threads_post_id, posted_at, deal_score,
                    price, text_hash, topic_tag, template_variant,
                    tip_id, content_trigger, search_keyword,
                    experiment_arm, assignment_key, experiment_epoch,
                    comic_id, comic_file, comic_stock_version,
                    media_url, media_hosting_provider,
                    comic_media_experiment_epoch,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO comic_usage(
                    comic_id, thread_post_id, item_code, category, selected_at,
                    published_at, assigned, delivered, selection_score,
                    selection_reason, comic_media_experiment_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, 'COMIC', 'COMIC', ?, ?, ?)
                """,
                (
                    comic_id, threads_post_id, item_code, search_keyword,
                    selected_at, posted_at, selection_score, selection_reason,
                    comic_media_experiment_epoch,
                ),
            )
