#!/usr/bin/env python3
"""Update insights for published Threads posts from the past 14 days."""

import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import (  # noqa: E402
    DATABASE_PATH,
    INSIGHTS_LOG_PATH,
    ConfigurationError,
    load_threads_access_token,
)
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402
from app.threads_insights import (  # noqa: E402
    ThreadsInsightsClient,
    ThreadsInsightsError,
)


LOGGER = logging.getLogger("threads_insights")


def configure_logging() -> None:
    INSIGHTS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        INSIGHTS_LOG_PATH, maxBytes=1_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.propagate = False


def main() -> int:
    configure_logging()
    try:
        token = load_threads_access_token()
        since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
        updated = failed = 0
        totals = {"views": 0, "likes": 0, "replies": 0}
        with Database(DATABASE_PATH) as database:
            initialize_database(database)
            rows = database.published_threads_posts_since(since)
            with ThreadsInsightsClient(token) as client:
                for row in rows:
                    post_id = str(row["threads_post_id"])
                    try:
                        insights = client.get_post_insights(post_id)
                        database.update_threads_insights(
                            post_id,
                            views=insights.views,
                            likes=insights.likes,
                            replies=insights.replies,
                            reposts=insights.reposts,
                            quotes=insights.quotes,
                            shares=insights.shares,
                            updated_at=datetime.now(timezone.utc).isoformat(),
                        )
                        try:
                            unique_repliers, conversation_depth = client.get_conversation_metrics(post_id)
                            database.update_threads_conversation_metrics(
                                post_id, unique_repliers, conversation_depth
                            )
                        except ThreadsInsightsError as error:
                            LOGGER.warning(
                                "Reply metrics unavailable post_id=%s error=%s", post_id, error
                            )
                        updated += 1
                        totals["views"] += insights.views or 0
                        totals["likes"] += insights.likes or 0
                        totals["replies"] += insights.replies or 0
                        LOGGER.info("Insights updated post_id=%s", post_id)
                    except ThreadsInsightsError as error:
                        failed += 1
                        LOGGER.error(
                            "Insights update failed post_id=%s error=%s",
                            post_id,
                            error,
                        )
        print(f"更新投稿数: {updated}")
        print(f"失敗投稿数: {failed}")
        print(f"合計views: {totals['views']}")
        print(f"合計likes: {totals['likes']}")
        print(f"合計replies: {totals['replies']}")
        return 0
    except (ConfigurationError, sqlite3.Error) as error:
        LOGGER.error("Insights job failed error_type=%s", type(error).__name__)
        print("Threads Insights更新を開始できませんでした", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
