#!/usr/bin/env python3
"""Prepare one Threads IMAGE post without calling any Threads endpoint."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.comics.media_hosting import (  # noqa: E402
    MEDIA_HOSTING_NOT_CONFIGURED,
    MediaHostingError,
    configured_provider,
    detect_existing_media_hosting,
)
from app.comics.media_publisher import (  # noqa: E402
    build_comic_alt_text,
    build_image_container_payload,
)
from app.comics.stock_selector import COMIC, ComicStockSelector  # noqa: E402
from app.config import (  # noqa: E402
    COMIC_MEDIA_HOSTING_PROVIDER,
    COMIC_STOCK_PUBLISHING_ENABLED,
    DATABASE_PATH,
)
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402
from app.publishers.threads import find_preview_candidates  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upload-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = datetime.now(timezone.utc)
    detection = detect_existing_media_hosting()
    with Database(DATABASE_PATH) as database:
        initialize_database(database)
        before_usage = database.connection.execute("SELECT COUNT(*) FROM comic_usage").fetchone()[0]
        candidates = find_preview_candidates(database, now=now)
        if not candidates:
            print("candidateなし")
            print("PUBLISH_CALLED=false")
            return 0
        candidate = candidates[0]
        selector = ComicStockSelector()
        selection = selector.select(
            item_code=candidate.product.item_code,
            search_keyword=candidate.search_keyword,
            category=candidate.search_keyword,
            topic_tag=candidate.topic_tag,
            post_type=candidate.template_variant,
            posting_date=now.date(),
            assigned_media_variant=COMIC,
        )
        asset = next(
            (item for item in selector.validation.valid_assets if item.comic_id == selection.comic_id),
            None,
        )
        public_url_status = MEDIA_HOSTING_NOT_CONFIGURED
        hosted = None
        if args.upload_test:
            if COMIC_MEDIA_HOSTING_PROVIDER == "disabled":
                print("UPLOAD_TEST_SKIPPED=MEDIA_HOSTING_NOT_CONFIGURED")
            elif asset is not None:
                try:
                    hosted = configured_provider().publish_asset(
                        str(asset.file_path), asset.comic_id, asset.sha256
                    )
                    public_url_status = hosted.integrity_status
                except MediaHostingError as error:
                    public_url_status = str(error)
        alt_text = build_comic_alt_text(
            asset.theme if asset else "PET_LIFE", candidate.search_keyword
        )
        payload = None
        if hosted is not None and not hosted.is_expired(now):
            payload = build_image_container_payload(
                text=candidate.text,
                topic_tag=candidate.topic_tag,
                public_image_url=hosted.public_url,
                alt_text=alt_text,
            )
        print(f"MEDIA_HOSTING_FOUND={str(detection.found).lower()}")
        print(f"provider={detection.provider}")
        print(f"configured_provider={COMIC_MEDIA_HOSTING_PROVIDER}")
        print(f"item_code={candidate.product.item_code}")
        print(f"category={candidate.search_keyword}")
        print(f"assigned_media_variant={selection.assigned_media_variant}")
        print(f"comic_id={selection.comic_id or 'NO_COMIC'}")
        print(f"comic_file={selection.file_path.name if selection.file_path else 'N/A'}")
        print(f"media_hosting_provider={hosted.provider if hosted else 'disabled'}")
        print(f"public_url_status={public_url_status}")
        print(f"alt_text={alt_text}")
        print("image_container_payload=" + (
            json.dumps(payload, ensure_ascii=False, indent=2)
            if payload else "NOT_BUILT: MEDIA_HOSTING_NOT_CONFIGURED"
        ))
        print(f"text_length={len(candidate.text)}")
        print(f"affiliate_url_present={str(bool(candidate.product.affiliate_url and candidate.product.affiliate_url in candidate.text)).lower()}")
        print(f"affiliate_disclosure_present={str('※本投稿にはアフィリエイトリンクが含まれます。' in candidate.text).lower()}")
        print(f"publishing_enabled={str(COMIC_STOCK_PUBLISHING_ENABLED).lower()}")
        print("THREADS_API_CALLED=false")
        print("PUBLISH_CALLED=false")
        after_usage = database.connection.execute("SELECT COUNT(*) FROM comic_usage").fetchone()[0]
        if before_usage != after_usage:
            raise RuntimeError("Preview unexpectedly changed production usage")
    return 0


if __name__ == "__main__":
    sys.exit(main())
