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

from app.collector import load_search_terms  # noqa: E402
from app.config import (  # noqa: E402
    COMIC_MEDIA_MIN_SAMPLE,
    DATABASE_PATH,
    SEARCH_TERMS_PATH,
)
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402


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


def nullable_average(value: object) -> str:
    return "N/A" if value is None else f"{float(value):.2f}"


def media_variant_rows(connection, since: str, column: str) -> list:
    if column not in {"assigned_media_variant", "delivered_media_variant"}:
        raise ValueError("Unsupported media variant dimension")
    return connection.execute(
        f"""
        SELECT {column} AS media_variant, COUNT(*) AS published_count,
               SUM(views) AS views_total, AVG(views) AS views_per_post,
               AVG(likes) AS likes_per_post, AVG(replies) AS replies_per_post,
               AVG(reposts) AS reposts_per_post, AVG(clicks) AS clicks_per_post,
               AVG(confirmed_orders) AS orders_per_post,
               AVG(confirmed_commission) AS commission_per_post
        FROM threads_posts
        WHERE status = 'published' AND posted_at >= ?
          AND {column} IN ('COMIC', 'NO_COMIC')
        GROUP BY {column}
        """,
        (since,),
    ).fetchall()


def print_media_variant_report(connection, since: str) -> None:
    counts = {}
    for label, column in (
        ("ITT（assigned基準）", "assigned_media_variant"),
        ("Delivered（配信実績基準）", "delivered_media_variant"),
    ):
        print(f"\nMedia Variant {label}")
        rows = {row["media_variant"]: row for row in media_variant_rows(connection, since, column)}
        for variant in ("COMIC", "NO_COMIC"):
            row = rows.get(variant)
            count = int(row["published_count"]) if row else 0
            if column == "assigned_media_variant":
                counts[variant] = count
            print(
                f"{variant}: published_count={count}, "
                f"views_total={number(row['views_total']) if row else 0}, "
                f"views/post={nullable_average(row['views_per_post']) if row else 'N/A'}, "
                f"likes/post={nullable_average(row['likes_per_post']) if row else 'N/A'}, "
                f"replies/post={nullable_average(row['replies_per_post']) if row else 'N/A'}, "
                f"reposts/post={nullable_average(row['reposts_per_post']) if row else 'N/A'}, "
                f"clicks/post={nullable_average(row['clicks_per_post']) if row else 'N/A'}, "
                f"orders/post={nullable_average(row['orders_per_post']) if row else 'N/A'}, "
                f"commission/post={nullable_average(row['commission_per_post']) if row else 'N/A'}"
            )
    readiness = (
        "SAMPLE_READY"
        if counts.get("COMIC", 0) >= COMIC_MEDIA_MIN_SAMPLE
        and counts.get("NO_COMIC", 0) >= COMIC_MEDIA_MIN_SAMPLE
        else "INSUFFICIENT_SAMPLE"
    )
    print(
        f"Media experiment readiness: {readiness} "
        f"(minimum={COMIC_MEDIA_MIN_SAMPLE}/arm)"
    )
    health = connection.execute(
        """
        SELECT
          SUM(CASE WHEN assigned_media_variant='COMIC' THEN 1 ELSE 0 END) comic_assigned,
          SUM(CASE WHEN delivered_media_variant='COMIC' THEN 1 ELSE 0 END) comic_delivered,
          SUM(CASE WHEN assigned_media_variant='COMIC' AND delivered_media_variant='NO_COMIC' THEN 1 ELSE 0 END) comic_fallback,
          SUM(CASE WHEN comic_fallback_reason='COMIC_SELECTION_FAILED' THEN 1 ELSE 0 END) selection_failures,
          SUM(CASE WHEN comic_fallback_reason='COMIC_MEDIA_FAILED' THEN 1 ELSE 0 END) media_failures
        FROM threads_posts WHERE status='published' AND posted_at >= ?
        """,
        (since,),
    ).fetchone()
    print(
        "Comic health: "
        f"assigned={number(health['comic_assigned'])}, "
        f"delivered={number(health['comic_delivered'])}, "
        f"fallback={number(health['comic_fallback'])}, "
        f"selection_failure={number(health['selection_failures'])}, "
        f"media_failure={number(health['media_failures'])}"
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

        print_media_variant_report(connection, since)

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
        for category in load_search_terms(SEARCH_TERMS_PATH):
            row = connection.execute(
                """
                SELECT COUNT(DISTINCT tp.id) AS posts,
                       AVG(tp.deal_score) AS avg_score,
                       COALESCE(SUM(tp.views), 0) AS total_views,
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
                f"平均Deal Score={avg_score}, views合計={number(row['total_views'])}, "
                f"平均views={avg_views}"
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

        print("\nComic別（分析のみ・Optimizer未使用）")
        comic_rows = connection.execute(
            """
            SELECT comic_id, COUNT(*) AS published_count,
                   AVG(views) AS views_per_post,
                   AVG(replies) AS replies_per_post,
                   AVG(reposts) AS reposts_per_post,
                   AVG(clicks) AS clicks_per_post,
                   AVG(confirmed_commission) AS commission_per_post
            FROM threads_posts
            WHERE status = 'published' AND posted_at >= ?
              AND delivered_media_variant = 'COMIC' AND comic_id IS NOT NULL
            GROUP BY comic_id ORDER BY published_count DESC, comic_id
            """,
            (since,),
        ).fetchall()
        if not comic_rows:
            print("配信済みcomic投稿なし")
        for row in comic_rows:
            print(
                f"{row['comic_id']}: published={row['published_count']}, "
                f"views/post={nullable_average(row['views_per_post'])}, "
                f"replies/post={nullable_average(row['replies_per_post'])}, "
                f"reposts/post={nullable_average(row['reposts_per_post'])}, "
                f"clicks/post={nullable_average(row['clicks_per_post'])}, "
                f"commission/post={nullable_average(row['commission_per_post'])}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
