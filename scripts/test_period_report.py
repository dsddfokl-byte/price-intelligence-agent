#!/usr/bin/env python3
"""Print a database-only performance report for the past 14 days."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import DATABASE_PATH  # noqa: E402
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402


CATEGORIES = ("猫 フード", "猫砂", "ペットシーツ", "犬 フード")


def number(value: object) -> int:
    return int(value or 0)


def print_insight_group(label: str, rows: list) -> None:
    views = [number(row["views"]) for row in rows]
    print(
        f"{label}: 投稿数={len(rows)}, views合計={sum(views)}, "
        f"平均views={(sum(views) / len(rows) if rows else 0):.2f}, "
        f"median views={median(views) if views else 0:.2f}, "
        f"likes={sum(number(row['likes']) for row in rows)}, "
        f"replies={sum(number(row['replies']) for row in rows)}, "
        f"reposts={sum(number(row['reposts']) for row in rows)}, "
        f"quotes={sum(number(row['quotes']) for row in rows)}, "
        f"shares={sum(number(row['shares']) for row in rows)}"
    )


def main() -> int:
    since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    with Database(DATABASE_PATH) as database:
        initialize_database(database)
        connection = database.connection
        monitoring = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM collections
                 WHERE status = 'success' AND started_at >= ?) AS api_calls,
                (SELECT COUNT(*) FROM products
                 WHERE last_seen_at >= ?) AS products,
                (SELECT COUNT(*) FROM price_history
                 WHERE fetched_at >= ?) AS price_rows
            """,
            (since, since, since),
        ).fetchone()
        threads = connection.execute(
            """
            SELECT COUNT(*) AS posts, COALESCE(SUM(views), 0) AS views,
                   COALESCE(AVG(views), 0) AS avg_views,
                   COALESCE(SUM(likes), 0) AS likes,
                   COALESCE(SUM(replies), 0) AS replies,
                   COALESCE(SUM(reposts), 0) AS reposts,
                   COALESCE(SUM(quotes), 0) AS quotes,
                   COALESCE(SUM(shares), 0) AS shares
            FROM threads_posts
            WHERE status = 'published' AND posted_at >= ?
            """,
            (since,),
        ).fetchone()

        print("期間: 過去14日")
        print("\n商品監視")
        print(f"API取得回数: {number(monitoring['api_calls'])}")
        print(f"products数: {number(monitoring['products'])}")
        print(f"price_history数: {number(monitoring['price_rows'])}")
        print("\nThreads")
        print(f"投稿数: {number(threads['posts'])}")
        print(f"views合計: {number(threads['views'])}")
        print(f"投稿あたり平均views: {float(threads['avg_views'] or 0):.2f}")
        print(f"likes: {number(threads['likes'])}")
        print(f"replies: {number(threads['replies'])}")
        print(f"reposts: {number(threads['reposts'])}")
        print(f"quotes: {number(threads['quotes'])}")
        print(f"shares: {number(threads['shares'])}")

        post_rows = connection.execute(
            """
            SELECT * FROM threads_posts
            WHERE status = 'published' AND posted_at >= ?
            """,
            (since,),
        ).fetchall()

        print("\nVariant別")
        for variant in ("PRICE_CONTROL", "OWNER_VALUE"):
            print_insight_group(
                variant,
                [row for row in post_rows if row["template_variant"] == variant],
            )

        print("\nTopic別")
        topics = sorted(
            {row["topic_tag"] for row in post_rows if row["topic_tag"] is not None}
        )
        if not topics:
            print("topic_tag付き投稿なし")
        for topic in topics:
            topic_rows = [row for row in post_rows if row["topic_tag"] == topic]
            total_views = sum(number(row["views"]) for row in topic_rows)
            print(
                f"{topic}: 投稿数={len(topic_rows)}, views合計={total_views}, "
                f"平均views={total_views / len(topic_rows):.2f}, "
                f"replies={sum(number(row['replies']) for row in topic_rows)}"
            )

        print("\nカテゴリー別")
        for category in CATEGORIES:
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT tp.id) AS posts,
                       AVG(tp.deal_score) AS avg_score,
                       AVG(tp.views) AS avg_views
                FROM threads_posts tp
                WHERE tp.status = 'published'
                  AND tp.posted_at >= ?
                  AND (
                      tp.search_keyword = ?
                      OR (
                          tp.search_keyword IS NULL
                          AND EXISTS (
                              SELECT 1 FROM product_keywords pk
                              WHERE pk.item_code = tp.item_code
                                AND pk.keyword = ?
                          )
                      )
                  )
                """,
                (since, category, category),
            ).fetchone()
            avg_score = (
                f"{float(row['avg_score']):.2f}"
                if row["avg_score"] is not None
                else "N/A"
            )
            avg_views = (
                f"{float(row['avg_views']):.2f}"
                if row["avg_views"] is not None
                else "N/A"
            )
            print(
                f"{category}: 投稿数={number(row['posts'])}, "
                f"平均Deal Score={avg_score}, 平均views={avg_views}"
            )

        print("\n時間帯別（Asia/Tokyo）")
        tokyo = ZoneInfo("Asia/Tokyo")
        for hour in (7, 12, 17, 21):
            hour_rows = []
            for row in post_rows:
                posted_at = datetime.fromisoformat(row["posted_at"])
                if posted_at.astimezone(tokyo).hour == hour:
                    hour_rows.append(row)
            total_views = sum(number(row["views"]) for row in hour_rows)
            average = total_views / len(hour_rows) if hour_rows else 0
            print(f"{hour:02d}時台: 投稿数={len(hour_rows)}, 平均views={average:.2f}")

        print("\n商品別TOP10（views降順）")
        rows = connection.execute(
            """
            SELECT tp.item_code, p.item_name, tp.views, tp.likes,
                   tp.replies, tp.deal_score
            FROM threads_posts tp
            LEFT JOIN products p ON p.item_code = tp.item_code
            WHERE tp.status = 'published' AND tp.posted_at >= ?
            ORDER BY COALESCE(tp.views, -1) DESC, tp.posted_at DESC
            LIMIT 10
            """,
            (since,),
        ).fetchall()
        if not rows:
            print("対象投稿なし")
        for index, row in enumerate(rows, 1):
            views = row["views"] if row["views"] is not None else "N/A"
            print(
                f"{index}. {row['item_code']} | {row['item_name'] or '商品名なし'} | "
                f"views={views} | likes={row['likes'] or 0} | "
                f"replies={row['replies'] or 0} | score={row['deal_score']:.2f}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
