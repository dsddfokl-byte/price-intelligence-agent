#!/usr/bin/env python3
"""Preview five comic IMAGE payloads; optionally create one unpublished container."""

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.comics.media_hosting import (  # noqa: E402
    GitHubPagesComicMediaHostingProvider,
    MediaHostingError,
    configured_provider,
)
from app.comics.media_publisher import (  # noqa: E402
    ComicThreadsPublisher,
    build_comic_alt_text,
    build_image_container_payload,
)
from app.comics.stock_selector import COMIC, ComicStockSelector, ComicUsageRecord  # noqa: E402
from app.config import (  # noqa: E402
    COMIC_MEDIA_HOSTING_PROVIDER,
    COMIC_STOCK_PUBLISHING_ENABLED,
    DATABASE_PATH,
    THREADS_IMAGE_CONTAINER_TEST_ENABLED,
    THREADS_PUBLISHING,
    THREADS_TOPIC_TAGS,
    load_threads_access_token,
)
from app.database import Database  # noqa: E402
from app.publishers.threads import (  # noqa: E402
    ThreadsCandidate,
    _build_candidate_content,
    _product_from_row,
)
from app.scoring import calculate_deal_score  # noqa: E402


TARGET_CATEGORIES = (
    "猫砂",
    "犬 おやつ",
    "猫 おもちゃ",
    "ペット 給水器",
    "ペットカメラ",
)
DISCLOSURE = "※本投稿にはアフィリエイトリンクが含まれます。"
CONTAINER_TEST_LIMIT = 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--create-container-test",
        action="store_true",
        help="Create one IMAGE container but never publish it",
    )
    return parser.parse_args()


def preview_candidates_by_category(
    database: Database, now: datetime
) -> List[ThreadsCandidate]:
    """Build one read-only content sample per requested category."""
    rows_by_category: Dict[str, list] = {category: [] for category in TARGET_CATEGORIES}
    for row in database.products_for_threads():
        category = row["search_keyword"]
        if category in rows_by_category and row["affiliate_url"]:
            rows_by_category[category].append(row)
    candidates: List[ThreadsCandidate] = []
    for category in TARGET_CATEGORIES:
        ranked = []
        for row in rows_by_category[category]:
            product = _product_from_row(row)
            score = calculate_deal_score(
                product,
                row["previous_price"],
                price_history_count=row["price_history_count"],
            )
            ranked.append((score, product.item_code, row, product))
        if not ranked:
            continue
        score, _, row, product = max(ranked, key=lambda value: (value[0], value[1]))
        candidates.append(
            _build_candidate_content(
                database,
                product,
                category,
                row["previous_price"],
                score,
                now,
                "画像投稿dry-run用preview（production投稿可否とは別）",
                config=THREADS_PUBLISHING,
            )
        )
    return candidates


def mask_identifier(identifier: str) -> str:
    if len(identifier) <= 10:
        return "[MASKED]"
    return f"{identifier[:6]}...{identifier[-4:]}"


def main() -> int:
    args = parse_args()
    now = datetime.now(timezone.utc)
    container_called = False
    publish_called = False
    created_id = None
    container_status = "NOT_EXECUTED"
    previews = []

    with Database(DATABASE_PATH) as database:
        before_posts = database.connection.execute("SELECT COUNT(*) FROM threads_posts").fetchone()[0]
        before_usage = database.connection.execute("SELECT COUNT(*) FROM comic_usage").fetchone()[0]
        before_daily = database.connection.execute(
            "SELECT COUNT(*) FROM threads_posts WHERE status = 'published'"
        ).fetchone()[0]
        candidates = preview_candidates_by_category(database, now)
        usages = tuple(
            ComicUsageRecord(
                comic_id=row["comic_id"],
                item_code=row["item_code"],
                category=row["category"],
                selected_at=datetime.fromisoformat(row["selected_at"]),
                published_at=(
                    datetime.fromisoformat(row["published_at"])
                    if row["published_at"] else None
                ),
            )
            for row in database.comic_usage_rows()
        )
        selector = ComicStockSelector()
        provider = configured_provider()
        if not isinstance(provider, GitHubPagesComicMediaHostingProvider):
            raise RuntimeError("github_pages media provider is not configured")

        for candidate in candidates:
            selection = selector.select(
                item_code=candidate.product.item_code,
                search_keyword=candidate.search_keyword,
                category=candidate.search_keyword,
                topic_tag=candidate.topic_tag,
                post_type=candidate.template_variant,
                posting_date=now.date(),
                usages=usages,
                assigned_media_variant=COMIC,
            )
            if selection.comic_id is None or selection.file_path is None:
                previews.append((candidate, selection, None, None, None))
                continue
            asset = next(
                item for item in selector.validation.valid_assets
                if item.comic_id == selection.comic_id
            )
            try:
                hosted = provider.resolve_asset(
                    selection.comic_id, validate_remote=True
                )
            except MediaHostingError:
                previews.append((candidate, selection, asset, None, None))
                continue
            alt_text = build_comic_alt_text(asset.theme, candidate.search_keyword)
            payload = build_image_container_payload(
                text=candidate.text,
                topic_tag=candidate.topic_tag,
                public_image_url=hosted.public_url,
                alt_text=alt_text,
            )
            previews.append((candidate, selection, asset, hosted, payload))

        dry_run_ok = (
            len(previews) == len(TARGET_CATEGORIES)
            and all(hosted is not None and payload is not None for _, _, _, hosted, payload in previews)
        )
        for candidate, selection, asset, hosted, payload in previews:
            print("---")
            print(f"item_code={candidate.product.item_code}")
            print(f"keyword={candidate.search_keyword}")
            print(f"category={candidate.search_keyword}")
            print(f"topic_tag={candidate.topic_tag}")
            print(f"assigned_media_variant={selection.assigned_media_variant}")
            print(f"delivered_media_variant={selection.delivered_media_variant}")
            print(f"comic_id={selection.comic_id or 'NO_COMIC'}")
            print(f"comic_file={selection.file_path.name if selection.file_path else 'N/A'}")
            print(f"comic_selection_score={selection.selection_score:.0f}")
            print(f"comic_selection_reason={selection.selection_reason}")
            print(f"media_provider={hosted.provider if hosted else COMIC_MEDIA_HOSTING_PROVIDER}")
            print(f"public_image_url={hosted.public_url if hosted else 'NO_COMIC'}")
            print(f"public_url_validation={hosted.integrity_status if hosted else 'FAILED'}")
            print(f"alt_text={payload['alt_text'] if payload else 'N/A'}")
            print("threads_text=" + candidate.text.replace("\n", "\\n"))
            print(f"text_length={len(candidate.text)}")
            print(f"affiliate_url_present={str(bool(candidate.product.affiliate_url and candidate.product.affiliate_url in candidate.text)).lower()}")
            print(f"affiliate_disclosure_present={str(DISCLOSURE in candidate.text).lower()}")
            print("container_payload=" + json.dumps(payload, ensure_ascii=False) if payload else "container_payload=NO_COMIC")

        if args.create_container_test:
            if COMIC_STOCK_PUBLISHING_ENABLED:
                raise RuntimeError("Production comic publishing must remain disabled")
            if not THREADS_IMAGE_CONTAINER_TEST_ENABLED:
                raise RuntimeError(
                    "Container test is disabled; set THREADS_IMAGE_CONTAINER_TEST_ENABLED=true explicitly"
                )
            if not dry_run_ok:
                raise RuntimeError("Container test blocked because dry-run validation failed")
            if CONTAINER_TEST_LIMIT != 1:
                raise RuntimeError("Container test safety limit is invalid")
            candidate, _, _, hosted, payload = previews[:CONTAINER_TEST_LIMIT][0]
            assert hosted is not None and payload is not None
            token = load_threads_access_token()
            with ComicThreadsPublisher(token) as publisher:
                created_id = publisher.create_image_container(
                    candidate.text,
                    hosted.public_url,
                    payload["alt_text"],
                    candidate.topic_tag,
                )
                container_called = True
                for delay in (0, 2, 4, 8, 16):
                    if delay:
                        time.sleep(delay)
                    container_status = publisher.get_container_status(created_id)
                    if container_status != "IN_PROGRESS":
                        break

        after_posts = database.connection.execute("SELECT COUNT(*) FROM threads_posts").fetchone()[0]
        after_usage = database.connection.execute("SELECT COUNT(*) FROM comic_usage").fetchone()[0]
        after_daily = database.connection.execute(
            "SELECT COUNT(*) FROM threads_posts WHERE status = 'published'"
        ).fetchone()[0]
        if (before_posts, before_usage, before_daily) != (after_posts, after_usage, after_daily):
            raise RuntimeError("Image preview unexpectedly changed production database")

    print("---")
    print(f"DRY_RUN_CASES={len(previews)}")
    print(f"DRY_RUN_OK={str(dry_run_ok).lower()}")
    print(f"COMIC_MEDIA_HOSTING_PROVIDER={COMIC_MEDIA_HOSTING_PROVIDER}")
    print(f"COMIC_STOCK_PUBLISHING_ENABLED={str(COMIC_STOCK_PUBLISHING_ENABLED).lower()}")
    print(f"THREADS_IMAGE_CONTAINER_TEST_ENABLED={str(THREADS_IMAGE_CONTAINER_TEST_ENABLED).lower()}")
    print(f"THREADS_CONTAINER_CALLED={str(container_called).lower()}")
    print(f"CONTAINER_ID={mask_identifier(created_id) if created_id else 'N/A'}")
    print(f"CONTAINER_STATUS={container_status}")
    print(f"THREADS_PUBLISH_CALLED={str(publish_called).lower()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
