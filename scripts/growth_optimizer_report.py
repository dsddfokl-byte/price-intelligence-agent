#!/usr/bin/env python3
"""Read-only Phase 1 Threads growth diagnostics report."""

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import (  # noqa: E402
    DATABASE_PATH,
    GROWTH_MIN_PRACTICAL_UPLIFT,
    GROWTH_MIN_RUNTIME_DAYS,
    GROWTH_MIN_SAMPLES_PER_ARM,
    GROWTH_OPTIMIZER_MODE,
    POST_INTENT_EXPERIMENT_ENABLED,
    POST_INTENT_EPOCH,
    POST_INTENT_SPLIT,
)
from app.database import Database  # noqa: E402
from app.growth_optimizer import (  # noqa: E402
    GrowthExperimentRegistry, diagnose, distribution_stats,
    evaluate_views_experiment,
)
from app.growth_controller import (  # noqa: E402
    BoundedGrowthController, bounded_auto_eligibility, current_policy_version,
)


def value(number: object, digits: int = 2) -> str:
    return "NULL" if number is None else f"{float(number):.{digits}f}"


def main() -> int:
    now = datetime.now(timezone.utc)
    with Database(DATABASE_PATH) as database:
        diagnosis = diagnose(database.connection, now)
        controller_decision = BoundedGrowthController(database.connection).evaluate(now)
        bounded_eligibility = bounded_auto_eligibility(database.connection)
        metrics = diagnosis.windows[7]
        try:
            active = GrowthExperimentRegistry(database.connection).active()
        except sqlite3.OperationalError:
            active = None
        replies_per_post = metrics.per_post.get("replies")
        clicks_per_post = metrics.per_post.get("clicks")
        orders_per_post = metrics.per_post.get("confirmed_orders")
        commission = metrics.totals.get("confirmed_commission")

        print(f"GROWTH_OPTIMIZER_MODE={GROWTH_OPTIMIZER_MODE}")
        print(f"POST_INTENT_EXPERIMENT_ENABLED={str(POST_INTENT_EXPERIMENT_ENABLED).lower()}")
        print(f"POST_INTENT_SPLIT={POST_INTENT_SPLIT}")
        print(f"POST_INTENT_EPOCH={POST_INTENT_EPOCH}")
        print(f"CURRENT_BOTTLENECK={diagnosis.current_bottleneck.value}")
        print(f"BOUNDED_AUTO_ELIGIBLE={str(bounded_eligibility.eligible).lower()}")
        print(f"CURRENT_POLICY_VERSION={current_policy_version(database.connection)}")
        print(f"NEXT_BOUNDED_EXPERIMENT={controller_decision.next_experiment or 'NONE'}")
        print(f"ROLLBACK_ENABLED=true")
        print(f"7D_POSTS={metrics.posts}")
        print(f"MEDIAN_VIEWS={value(metrics.distribution.median)}")
        print(f"P75_VIEWS={value(metrics.distribution.p75)}")
        print(f"REPLIES_PER_POST={value(replies_per_post)}")
        print(f"CLICKS_PER_POST={value(clicks_per_post)}")
        print(f"ORDERS_PER_POST={value(orders_per_post)}")
        print(
            "COMMISSION_PER_DAY="
            + ("NULL" if commission is None else f"{commission / 7:.2f}")
        )
        print(
            "ACTIVE_EXPERIMENT="
            + (str(active["experiment_id"]) if active is not None else "NONE")
        )
        print(
            "SAMPLE_PROGRESS="
            + (
                f"registry_status={active['status']}"
                if active is not None else "NO_PRODUCTION_EXPERIMENT"
            )
        )
        print(
            "DECISION_STATUS="
            + (str(active["decision"] or active["status"]) if active else "SHADOW_ONLY")
        )
        print(f"NEXT_EXPERIMENT_PROPOSAL={diagnosis.next_experiment_proposal}")
        print(f"REASON={diagnosis.reason}")
        print(f"MIN_SAMPLES_PER_ARM={GROWTH_MIN_SAMPLES_PER_ARM}")
        print(f"MIN_RUNTIME_DAYS={GROWTH_MIN_RUNTIME_DAYS}")
        print(f"MIN_PRACTICAL_UPLIFT={GROWTH_MIN_PRACTICAL_UPLIFT:.2f}")

        print("\nWINDOWS")
        for days, window in diagnosis.windows.items():
            distribution = window.distribution
            label = "24h" if days == 1 else f"{days}d"
            print(
                f"{label} posts={window.posts} observed_views={distribution.count} "
                f"mean={value(distribution.mean)} median={value(distribution.median)} "
                f"p25={value(distribution.p25)} p75={value(distribution.p75)} "
                f"p90={value(distribution.p90)}"
            )
        print("\nDIMENSIONS_7D")
        for dimension, groups in metrics.dimensions.items():
            for name, stats in sorted(groups.items()):
                print(
                    f"{dimension}={name} posts={stats.posts} "
                    f"observed_views={stats.observed_views} "
                    f"median_views={value(stats.median_views)}"
                )
        print("\nPOST_INTENT_DELIVERED_14D")
        intent_rows = database.connection.execute(
            """
            SELECT COALESCE(delivered_post_intent, post_intent) intent,
                   COUNT(*) posts, AVG(replies) replies_per_post,
                   AVG(reposts) reposts_per_post, AVG(clicks) clicks_per_post,
                   AVG(confirmed_orders) orders_per_post,
                   AVG(confirmed_commission) commission_per_post
            FROM threads_posts WHERE status='published' AND posted_at>=?
              AND COALESCE(delivered_post_intent, post_intent)
                  IN ('GROWTH','AFFILIATE')
            GROUP BY COALESCE(delivered_post_intent, post_intent)
            """, ((now - timedelta(days=14)).isoformat(),)
        ).fetchall()
        intent_views = {}
        for row in intent_rows:
            values = [float(item[0]) for item in database.connection.execute(
                "SELECT views FROM threads_posts WHERE status='published' AND posted_at>=? AND COALESCE(delivered_post_intent, post_intent)=? AND views IS NOT NULL",
                ((now - timedelta(days=14)).isoformat(), row["intent"]),
            )]
            intent_views[row["intent"]] = values
            dist = distribution_stats(values)
            print(
                f"{row['intent']}_DELIVERED count={row['posts']} median_views={value(dist.median)} "
                f"p75_views={value(dist.p75)} replies/post={value(row['replies_per_post'])} "
                f"reposts/post={value(row['reposts_per_post'])} clicks/post={value(row['clicks_per_post'])} "
                f"orders/post={value(row['orders_per_post'])} commission/post={value(row['commission_per_post'])}"
            )
        experiment = database.connection.execute(
            "SELECT * FROM growth_experiments WHERE experiment_family='POST_INTENT' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        runtime_days = 0.0
        if experiment and experiment["started_at"]:
            runtime_days = max(0.0, (now - datetime.fromisoformat(experiment["started_at"])).total_seconds() / 86400)
        assigned_intent_views = {}
        for intent in ("AFFILIATE", "GROWTH"):
            assigned_intent_views[intent] = [
                float(item[0]) for item in database.connection.execute(
                    """SELECT views FROM threads_posts
                       WHERE status='published' AND posted_at>=?
                         AND COALESCE(assigned_post_intent, post_intent)=?
                         AND views IS NOT NULL""",
                    ((now - timedelta(days=14)).isoformat(), intent),
                )
            ]
        evaluation = evaluate_views_experiment(
            assigned_intent_views.get("AFFILIATE", []),
            assigned_intent_views.get("GROWTH", []),
            runtime_days,
        )
        print(f"POST_INTENT_DECISION_STATUS={evaluation.status.value}")
        print(f"POST_INTENT_DECISION={evaluation.decision}")
        print("POST_INTENT_DECISION_BASIS=ITT_ASSIGNED")
        print("\nPOST_INTENT_ASSIGNMENT_DELIVERY_14D")
        intent_summary = database.connection.execute(
            """
            SELECT
              SUM(CASE WHEN COALESCE(assigned_post_intent, post_intent)='AFFILIATE' THEN 1 ELSE 0 END) affiliate_assigned,
              SUM(CASE WHEN COALESCE(assigned_post_intent, post_intent)='GROWTH' THEN 1 ELSE 0 END) growth_assigned,
              SUM(CASE WHEN COALESCE(delivered_post_intent, post_intent)='AFFILIATE' THEN 1 ELSE 0 END) affiliate_delivered,
              SUM(CASE WHEN COALESCE(delivered_post_intent, post_intent)='GROWTH' THEN 1 ELSE 0 END) growth_delivered,
              SUM(CASE WHEN assigned_post_intent='AFFILIATE' AND delivered_post_intent='GROWTH' THEN 1 ELSE 0 END) fallback_count,
              SUM(CASE WHEN assigned_post_intent='AFFILIATE' THEN 1 ELSE 0 END) explicit_affiliate_assigned
            FROM threads_posts WHERE status='published' AND posted_at>=?
            """,
            ((now - timedelta(days=14)).isoformat(),),
        ).fetchone()
        fallback_count = int(intent_summary["fallback_count"] or 0)
        assigned_count = int(intent_summary["explicit_affiliate_assigned"] or 0)
        fallback_rate = fallback_count / assigned_count if assigned_count else None
        print(f"AFFILIATE_ASSIGNED={int(intent_summary['affiliate_assigned'] or 0)}")
        print(f"GROWTH_ASSIGNED={int(intent_summary['growth_assigned'] or 0)}")
        print(f"AFFILIATE_DELIVERED={int(intent_summary['affiliate_delivered'] or 0)}")
        print(f"GROWTH_DELIVERED={int(intent_summary['growth_delivered'] or 0)}")
        print(f"AFFILIATE_TO_GROWTH_FALLBACK_COUNT={fallback_count}")
        print(f"AFFILIATE_TO_GROWTH_FALLBACK_RATE={value(fallback_rate)}")
        failed_fallbacks = database.connection.execute(
            """SELECT COUNT(*) FROM threads_posts
               WHERE status IN ('failed','skipped') AND posted_at>=?
                 AND assigned_post_intent='AFFILIATE'
                 AND post_intent_fallback_reason='AFFILIATE_NO_ELIGIBLE_PRODUCT'""",
            ((now - timedelta(days=14)).isoformat(),),
        ).fetchone()[0]
        print(f"AFFILIATE_NO_PRODUCT_SKIP_COUNT={int(failed_fallbacks)}")
        print("\nPOST_INTENT_4_CELLS_14D")
        rows = database.connection.execute(
            """
            SELECT post_intent, delivered_media_variant, COUNT(*) posts,
                   AVG(views) views_per_post, AVG(replies) replies_per_post,
                   AVG(reposts) reposts_per_post, AVG(clicks) clicks_per_post,
                   AVG(confirmed_orders) orders_per_post,
                   AVG(confirmed_commission) commission_per_post
            FROM threads_posts
            WHERE status='published' AND posted_at>=? AND post_intent IS NOT NULL
            GROUP BY post_intent, delivered_media_variant
            ORDER BY post_intent, delivered_media_variant
            """,
            ((now - timedelta(days=14)).isoformat(),),
        ).fetchall()
        for row in rows:
            cell_views = database.connection.execute(
                "SELECT views FROM threads_posts WHERE status='published' AND posted_at>=? AND post_intent=? AND delivered_media_variant=? AND views IS NOT NULL ORDER BY views",
                ((now - timedelta(days=14)).isoformat(), row["post_intent"], row["delivered_media_variant"]),
            ).fetchall()
            view_values = [float(item[0]) for item in cell_views]
            dist = distribution_stats(view_values)
            print(
                f"{row['post_intent']} × {row['delivered_media_variant']} "
                f"count={row['posts']} median_views={value(dist.median)} p75_views={value(dist.p75)} "
                f"replies/post={value(row['replies_per_post'])} reposts/post={value(row['reposts_per_post'])} "
                f"clicks/post={value(row['clicks_per_post'])} orders/post={value(row['orders_per_post'])} "
                f"commission/post={value(row['commission_per_post'])}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
