#!/usr/bin/env python3
"""Publish one explicitly selected product to Threads."""

import argparse
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import (  # noqa: E402
    ConfigurationError,
    LOG_PATH,
    THREADS_PUBLISHING,
    load_settings,
    load_threads_access_token,
)
from app.autopilot import (  # noqa: E402
    AutopilotController,
    HaltClass,
    StateValidationError,
    get_state,
    emergency_safe_halt,
    new_controller_evaluation_id,
    runtime_policy,
    system_health_errors,
    validate_publish_payload,
)
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402
from app.publishers.threads import (  # noqa: E402
    ThreadsAPIError,
    ThreadsPublisher,
    daily_period_start,
    evaluate_product,
)


LOGGER = logging.getLogger("threads_publisher")


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.setLevel(logging.DEBUG)
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.propagate = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Publish one eligible Rakuten product to Threads"
    )
    parser.add_argument("item_code", help="Rakuten item_code to publish")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    configure_logging()
    try:
        settings = load_settings()
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    with Database(settings.database_path) as database:
        initialize_database(database)
        try:
            state = get_state(database.connection)
        except StateValidationError:
            state = emergency_safe_halt(
                database.connection,
                new_controller_evaluation_id(),
                "State machine corruption",
                now=now,
            )
        health_errors = system_health_errors(
            database.connection, now, THREADS_PUBLISHING.daily_post_limit
        )
        if health_errors:
            state = AutopilotController(database.connection).evaluate(
                new_controller_evaluation_id(),
                now=now,
                global_safety_error="; ".join(health_errors),
                halt_class=HaltClass.MANUAL_REVIEW_REQUIRED,
            )
        if runtime_policy(state).halt_publish:
            print("SAFE_HALTまたはruntime kill switchにより投稿を停止しました。", file=sys.stderr)
            return 1
        row = database.product_for_threads(args.item_code)
        if row is None:
            print("指定されたitem_codeの商品はDBに存在しません。", file=sys.stderr)
            return 1

        posted_today = database.published_threads_count_since(
            daily_period_start(now, THREADS_PUBLISHING.daily_timezone).isoformat()
        )
        if posted_today >= THREADS_PUBLISHING.daily_post_limit:
            print("1日の投稿上限に達しているため投稿しません。", file=sys.stderr)
            return 1

        candidate, reason = evaluate_product(database, row, now=now)
        if candidate is None:
            print(f"投稿条件を満たしません: {reason}", file=sys.stderr)
            return 1

        try:
            validate_publish_payload(candidate.text, candidate.product.affiliate_url)
        except StateValidationError as error:
            AutopilotController(database.connection).evaluate(
                new_controller_evaluation_id(),
                now=now,
                global_safety_error=str(error),
                halt_class=HaltClass.MANUAL_REVIEW_REQUIRED,
            )
            print("安全性検証に失敗したためSAFE_HALTへ移行しました。", file=sys.stderr)
            return 1

        try:
            access_token = load_threads_access_token()
            with ThreadsPublisher(access_token) as publisher:
                post_id = publisher.publish_text(
                    candidate.text, topic_tag=candidate.topic_tag
                )
        except (ConfigurationError, ThreadsAPIError) as error:
            safe_error = str(error)
            database.record_threads_post(
                item_code=candidate.product.item_code,
                threads_post_id=None,
                posted_at=now.isoformat(),
                deal_score=candidate.deal_score,
                price=candidate.product.item_price,
                text_hash=candidate.text_hash,
                status="failed",
                error=safe_error,
                topic_tag=candidate.topic_tag,
                template_variant=candidate.template_variant,
                tip_id=candidate.tip_id,
                content_trigger=candidate.content_trigger,
                search_keyword=candidate.search_keyword,
            )
            LOGGER.error(
                "Threads publish failed for item_code=%s: %s",
                candidate.product.item_code,
                safe_error,
            )
            print(f"Threads投稿に失敗しました: {safe_error}", file=sys.stderr)
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
            "Threads publish succeeded for item_code=%s post_id=%s",
            candidate.product.item_code,
            post_id,
        )
        print(f"Threads投稿成功: post_id={post_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
