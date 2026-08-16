#!/usr/bin/env python3
"""Run one collection-to-Threads publishing cycle."""

import argparse
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.collector import collect_products, load_search_terms  # noqa: E402
from app.config import (  # noqa: E402
    AUTOMATION_LOG_PATH,
    ConfigurationError,
    RUN_CYCLE_LOCK_PATH,
    SEARCH_TERMS_PATH,
    THREADS_PUBLISHING,
    load_settings,
    load_threads_access_token,
)
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402
from app.publishers.threads import (  # noqa: E402
    ThreadsAPIError,
    ThreadsPublisher,
    daily_period_start,
    find_publishable_candidates,
)
from app.rakuten_client import RakutenAPIError, RakutenClient  # noqa: E402
from app.run_lock import CycleLock, LockAlreadyHeld  # noqa: E402


LOGGER = logging.getLogger("affiliate_automation")


def configure_logging() -> None:
    AUTOMATION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        AUTOMATION_LOG_PATH,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.propagate = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one affiliate automation cycle")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect and preview without publishing to Threads",
    )
    parser.add_argument(
        "--no-collect",
        action="store_true",
        help="select a candidate from the existing database",
    )
    return parser.parse_args()


def run_cycle(args: argparse.Namespace) -> int:
    LOGGER.info(
        "Cycle started dry_run=%s no_collect=%s",
        args.dry_run,
        args.no_collect,
    )
    lock = CycleLock(RUN_CYCLE_LOCK_PATH)
    try:
        lock.acquire()
    except LockAlreadyHeld as error:
        LOGGER.info("Cycle skipped reason=%s", error)
        LOGGER.info("Cycle finished status=skipped")
        return 0
    except OSError:
        LOGGER.error("Cycle failed lock_error")
        LOGGER.info("Cycle finished status=failed")
        return 1

    try:
        try:
            settings = load_settings()
            search_terms = (
                [] if args.no_collect else load_search_terms(SEARCH_TERMS_PATH)
            )
        except (ConfigurationError, RuntimeError) as error:
            LOGGER.error("Cycle failed configuration_error=%s", error)
            return 1

        try:
            with Database(settings.database_path) as database:
                initialize_database(database)

                if not args.no_collect:
                    with RakutenClient(settings) as client:
                        result = collect_products(
                            database,
                            client,
                            search_terms,
                            LOGGER,
                            hits=30,
                        )
                    LOGGER.info("API fetched item_count=%d", result.fetched_count)
                    LOGGER.info("Products updated item_count=%d", result.updated_count)
                else:
                    LOGGER.info("Collection skipped reason=--no-collect")

                now = datetime.now(timezone.utc)
                day_start = daily_period_start(
                    now,
                    THREADS_PUBLISHING.daily_timezone,
                )
                posted_today = database.published_threads_count_since(
                    day_start.isoformat()
                )
                if posted_today >= THREADS_PUBLISHING.daily_post_limit:
                    LOGGER.info(
                        "Post skipped reason=daily_limit posted_count=%d limit=%d timezone=%s",
                        posted_today,
                        THREADS_PUBLISHING.daily_post_limit,
                        THREADS_PUBLISHING.daily_timezone,
                    )
                    return 0

                candidates = find_publishable_candidates(
                    database,
                    THREADS_PUBLISHING,
                    now,
                )
                LOGGER.info("Candidates selected candidate_count=%d", len(candidates))
                if not candidates:
                    LOGGER.info("Post skipped reason=no_publishable_candidate")
                    return 0

                candidate = candidates[0]
                LOGGER.info(
                    "Post target item_code=%s deal_score=%.2f",
                    candidate.product.item_code,
                    candidate.deal_score,
                )

                if args.dry_run:
                    LOGGER.info("Post skipped reason=dry_run")
                    print("DRY RUN: Threadsへは投稿しません")
                    print(f"item_code: {candidate.product.item_code}")
                    print(f"deal_score: {candidate.deal_score:.2f}")
                    print(f"topic_tag: {candidate.topic_tag}")
                    print(f"template_variant: {candidate.template_variant}")
                    print(f"tip_id: {candidate.tip_id or 'N/A'}")
                    print(f"content_trigger: {candidate.content_trigger or 'N/A'}")
                    print(candidate.text)
                    return 0

                try:
                    token = load_threads_access_token()
                    with ThreadsPublisher(token) as publisher:
                        post_id = publisher.publish_text(
                            candidate.text, topic_tag=candidate.topic_tag
                        )
                except (ConfigurationError, ThreadsAPIError) as error:
                    database.record_threads_post(
                        item_code=candidate.product.item_code,
                        threads_post_id=None,
                        posted_at=now.isoformat(),
                        deal_score=candidate.deal_score,
                        price=candidate.product.item_price,
                        text_hash=candidate.text_hash,
                        status="failed",
                        error=str(error),
                        topic_tag=candidate.topic_tag,
                        template_variant=candidate.template_variant,
                        tip_id=candidate.tip_id,
                        content_trigger=candidate.content_trigger,
                        search_keyword=candidate.search_keyword,
                    )
                    LOGGER.error(
                        "Threads publish failed item_code=%s error=%s",
                        candidate.product.item_code,
                        error,
                    )
                    return 1

                database.record_threads_post(
                    item_code=candidate.product.item_code,
                    threads_post_id=post_id,
                    posted_at=now.isoformat(),
                    deal_score=candidate.deal_score,
                    price=candidate.product.item_price,
                    text_hash=candidate.text_hash,
                    status="published",
                    topic_tag=candidate.topic_tag,
                    template_variant=candidate.template_variant,
                    tip_id=candidate.tip_id,
                    content_trigger=candidate.content_trigger,
                    search_keyword=candidate.search_keyword,
                )
                LOGGER.info(
                    "Threads publish succeeded item_code=%s deal_score=%.2f post_id=%s",
                    candidate.product.item_code,
                    candidate.deal_score,
                    post_id,
                )
                return 0
        except RakutenAPIError as error:
            LOGGER.error("Cycle failed rakuten_api_error=%s", error)
            return 1
        except sqlite3.Error:
            LOGGER.error("Cycle failed database_error")
            return 1
        except Exception as error:
            LOGGER.error("Cycle failed unexpected_error_type=%s", type(error).__name__)
            return 1
    finally:
        lock.release()
        LOGGER.info("Cycle finished")


def main() -> int:
    configure_logging()
    return run_cycle(parse_args())


if __name__ == "__main__":
    sys.exit(main())
