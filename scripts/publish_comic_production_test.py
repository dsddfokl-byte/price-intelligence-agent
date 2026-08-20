#!/usr/bin/env python3
"""Publish exactly one production-selected comic IMAGE post for final validation."""

import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.autopilot import StateValidationError, validate_publish_payload  # noqa: E402
from app.comics.media_hosting import (  # noqa: E402
    GitHubPagesComicMediaHostingProvider,
    configured_provider,
)
from app.comics.media_publisher import ComicThreadsPublisher, build_comic_alt_text  # noqa: E402
from app.comics.stock_selector import COMIC, ComicStockSelector, ComicUsageRecord  # noqa: E402
from app.config import (  # noqa: E402
    COMIC_MEDIA_HOSTING_PROVIDER,
    COMIC_PRODUCTION_TEST_LIMIT,
    COMIC_STOCK_PUBLISHING_ENABLED,
    DATABASE_PATH,
    THREADS_IMAGE_CONTAINER_TEST_ENABLED,
    THREADS_PUBLISHING,
    THREADS_PUBLISH_CALL_LIMIT,
    load_threads_access_token,
)
from app.database import Database  # noqa: E402
from app.publishers.threads import (  # noqa: E402
    ThreadsAPIError,
    daily_period_start,
    find_publishable_candidates,
)


DISCLOSURE = "※本投稿にはアフィリエイトリンクが含まれます。"


def main() -> int:
    if COMIC_MEDIA_HOSTING_PROVIDER != "github_pages":
        print("SAFETY_CHECK_FAILED=media provider", file=sys.stderr)
        return 1
    if not COMIC_STOCK_PUBLISHING_ENABLED:
        print("SAFETY_CHECK_FAILED=comic publishing flag", file=sys.stderr)
        return 1
    if THREADS_IMAGE_CONTAINER_TEST_ENABLED:
        print("SAFETY_CHECK_FAILED=container test flag", file=sys.stderr)
        return 1
    if COMIC_PRODUCTION_TEST_LIMIT != 1 or THREADS_PUBLISH_CALL_LIMIT != 1:
        print("SAFETY_CHECK_FAILED=single publish limit", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    publish_calls = 0
    with Database(DATABASE_PATH) as database:
        before_posts = database.connection.execute("SELECT COUNT(*) FROM threads_posts").fetchone()[0]
        before_published = database.connection.execute(
            "SELECT COUNT(*) FROM threads_posts WHERE status = 'published'"
        ).fetchone()[0]
        before_usage = database.connection.execute("SELECT COUNT(*) FROM comic_usage").fetchone()[0]
        day_start = daily_period_start(now).isoformat()
        before_daily = database.published_threads_count_since(day_start)
        if before_daily >= THREADS_PUBLISHING.daily_post_limit:
            print("NO_ELIGIBLE_PRODUCTION_CANDIDATE=daily limit")
            return 0

        candidates = find_publishable_candidates(database, now=now)
        if not candidates:
            print("NO_ELIGIBLE_PRODUCTION_CANDIDATE=true")
            return 0
        candidate = candidates[0]
        if candidate.deal_score < THREADS_PUBLISHING.minimum_deal_score:
            print("SAFETY_CHECK_FAILED=Deal Score", file=sys.stderr)
            return 1
        if (
            not candidate.product.affiliate_url
            or candidate.product.affiliate_url not in candidate.text
            or DISCLOSURE not in candidate.text
            or not 0 < len(candidate.text) <= THREADS_PUBLISHING.maximum_text_length
        ):
            print("SAFETY_CHECK_FAILED=post payload", file=sys.stderr)
            return 1
        try:
            validate_publish_payload(candidate.text, candidate.product.affiliate_url)
        except StateValidationError:
            print("SAFETY_CHECK_FAILED=hard payload validation", file=sys.stderr)
            return 1

        usages = tuple(
            ComicUsageRecord(
                comic_id=row["comic_id"], item_code=row["item_code"],
                category=row["category"],
                selected_at=datetime.fromisoformat(row["selected_at"]),
                published_at=(datetime.fromisoformat(row["published_at"])
                              if row["published_at"] else None),
            )
            for row in database.comic_usage_rows()
        )
        selector = ComicStockSelector()
        selection = selector.select(
            item_code=candidate.product.item_code,
            search_keyword=candidate.search_keyword,
            category=candidate.search_keyword,
            topic_tag=candidate.topic_tag,
            post_type=candidate.template_variant,
            posting_date=now.date(),
            usages=usages,
        )
        if selection.assigned_media_variant != COMIC or not selection.comic_id:
            print("NO_ELIGIBLE_PRODUCTION_CANDIDATE=NO_COMIC assignment")
            return 0
        asset = next(
            item for item in selector.validation.valid_assets
            if item.comic_id == selection.comic_id
        )
        category_score = asset.category_scores.get(candidate.search_keyword, 0)
        if category_score < 100:
            print("SAFETY_CHECK_FAILED=comic category relevance", file=sys.stderr)
            return 1
        provider = configured_provider()
        if not isinstance(provider, GitHubPagesComicMediaHostingProvider):
            print("SAFETY_CHECK_FAILED=github_pages provider", file=sys.stderr)
            return 1
        hosted = provider.resolve_asset(selection.comic_id, validate_remote=True)
        if hosted.comic_id != selection.comic_id or hosted.public_url != provider.expected_public_url(selection.comic_id):
            print("SAFETY_CHECK_FAILED=public manifest mapping", file=sys.stderr)
            return 1
        alt_text = build_comic_alt_text(asset.theme, candidate.search_keyword)

        token = load_threads_access_token()
        selected_at = datetime.now(timezone.utc).isoformat()
        try:
            with ComicThreadsPublisher(token) as publisher:
                creation_id = publisher.create_image_container(
                    candidate.text, hosted.public_url, alt_text, candidate.topic_tag
                )
                container_status = "IN_PROGRESS"
                for delay in (0, 2, 4, 8, 16):
                    if delay:
                        time.sleep(delay)
                    container_status = publisher.get_container_status(creation_id)
                    if container_status != "IN_PROGRESS":
                        break
                if container_status != "FINISHED":
                    print(f"CONTAINER_PROCESSING_FAILED={container_status}", file=sys.stderr)
                    return 1
                if publish_calls >= THREADS_PUBLISH_CALL_LIMIT:
                    print("SAFETY_CHECK_FAILED=publish call limit", file=sys.stderr)
                    return 1
                publish_calls += 1
                post_id = publisher.publish_finished_container_once(
                    creation_id, container_status
                )
                published_at = datetime.now(timezone.utc).isoformat()
                database.record_published_comic_post(
                    item_code=candidate.product.item_code,
                    threads_post_id=post_id,
                    posted_at=published_at,
                    deal_score=candidate.deal_score,
                    price=candidate.product.item_price,
                    text_hash=candidate.text_hash,
                    topic_tag=candidate.topic_tag,
                    template_variant=candidate.template_variant,
                    tip_id=candidate.tip_id,
                    content_trigger=candidate.content_trigger,
                    search_keyword=candidate.search_keyword,
                    comic_id=selection.comic_id,
                    comic_file=(selection.file_path.name
                                if selection.file_path else asset.file),
                    comic_stock_version=selection.stock_version,
                    media_url=hosted.public_url,
                    media_hosting_provider=hosted.provider,
                    selected_at=selected_at,
                    selection_score=selection.selection_score,
                    selection_reason=selection.selection_reason,
                )
                details = publisher.get_post_details(post_id)
        except ThreadsAPIError as error:
            print(f"THREADS_IMAGE_PUBLISH_FAILED={error}", file=sys.stderr)
            return 1

        remote_text = details.get("text")
        media_type = str(details.get("media_type") or "")
        media_url = details.get("media_url")
        permalink = details.get("permalink")
        image_attached = bool(media_url) and "IMAGE" in media_type.upper()
        text_present = remote_text == candidate.text
        affiliate_present = bool(candidate.product.affiliate_url and remote_text and candidate.product.affiliate_url in remote_text)
        disclosure_present = bool(remote_text and DISCLOSURE in remote_text)
        if not all((details.get("id") == post_id, image_attached, text_present,
                    affiliate_present, disclosure_present, permalink)):
            print("POST_VERIFICATION_FAILED=true", file=sys.stderr)
            return 1

        after_posts = database.connection.execute("SELECT COUNT(*) FROM threads_posts").fetchone()[0]
        after_published = database.connection.execute(
            "SELECT COUNT(*) FROM threads_posts WHERE status = 'published'"
        ).fetchone()[0]
        after_usage = database.connection.execute("SELECT COUNT(*) FROM comic_usage").fetchone()[0]
        after_daily = database.published_threads_count_since(day_start)
        stored_post_count = database.connection.execute(
            "SELECT COUNT(*) FROM threads_posts WHERE threads_post_id = ?",
            (post_id,),
        ).fetchone()[0]
        insights_ready = any(
            row["threads_post_id"] == post_id
            for row in database.published_threads_posts_since(
                (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
            )
        )
        if (after_posts - before_posts, after_published - before_published,
                after_usage - before_usage, after_daily - before_daily) != (1, 1, 1, 1) or stored_post_count != 1:
            print("POST_PERSISTENCE_VALIDATION_FAILED=true", file=sys.stderr)
            return 1

        print("PRODUCTION_TEST_EXECUTED=true")
        print("ELIGIBLE_CANDIDATE_FOUND=true")
        print(f"ITEM_CODE={candidate.product.item_code}")
        print(f"CATEGORY={candidate.search_keyword}")
        print(f"DEAL_SCORE={candidate.deal_score:.2f}")
        print(f"COMIC_ID={selection.comic_id}")
        print(f"COMIC_FILE={selection.file_path.name if selection.file_path else asset.file}")
        print(f"CATEGORY_SCORE={category_score}")
        print(f"COMIC_SELECTION_SCORE={selection.selection_score:.0f}")
        print(f"COMIC_SELECTION_REASON={selection.selection_reason}")
        print(f"LAST_USED_AT={selection.last_used_at.isoformat() if selection.last_used_at else 'N/A'}")
        print(f"COMIC_PUBLIC_URL={hosted.public_url}")
        print("IMAGE_URL_VALID=true")
        print("CONTAINER_CREATED=true")
        print(f"CONTAINER_STATUS={container_status}")
        print(f"THREADS_PUBLISH_CALLS={publish_calls}")
        print("PUBLISHED=true")
        print(f"THREAD_POST_ID={post_id}")
        print(f"PERMALINK={permalink}")
        print(f"IMAGE_ATTACHED={str(image_attached).lower()}")
        print(f"TEXT_PRESENT={str(text_present).lower()}")
        print(f"AFFILIATE_URL_PRESENT={str(affiliate_present).lower()}")
        print(f"DISCLOSURE_PRESENT={str(disclosure_present).lower()}")
        print(f"THREADS_POSTS_BEFORE={before_posts}")
        print(f"THREADS_POSTS_AFTER={after_posts}")
        print(f"PUBLISHED_COUNT_BEFORE={before_published}")
        print(f"PUBLISHED_COUNT_AFTER={after_published}")
        print(f"COMIC_USAGE_BEFORE={before_usage}")
        print(f"COMIC_USAGE_AFTER={after_usage}")
        print(f"DAILY_COUNTER_DELTA={after_daily - before_daily}")
        print("CYCLE_COUNTER_DELTA=1")
        print("DUPLICATE=false")
        print(f"INSIGHTS_TRACKING_READY={str(insights_ready).lower()}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
