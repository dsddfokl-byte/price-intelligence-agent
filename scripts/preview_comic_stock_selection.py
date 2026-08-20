#!/usr/bin/env python3
"""Preview comic assignments without API calls or usage writes."""

import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.comics.stock_selector import (  # noqa: E402
    ComicStockSelector,
    ComicUsageRecord,
)
from app.config import (  # noqa: E402
    COMIC_STOCK_ENABLED,
    COMIC_STOCK_PUBLISHING_ENABLED,
    COMIC_MEDIA_HOSTING_STATUS,
    DATABASE_PATH,
)
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402
from app.publishers.threads import find_preview_candidates  # noqa: E402


def main() -> int:
    now = datetime.now(timezone.utc)
    with Database(DATABASE_PATH) as database:
        initialize_database(database)
        before = database.connection.execute("SELECT COUNT(*) FROM comic_usage").fetchone()[0]
        candidates = find_preview_candidates(database, now=now)
        usages = tuple(
            ComicUsageRecord(
                comic_id=row["comic_id"], item_code=row["item_code"],
                category=row["category"],
                selected_at=datetime.fromisoformat(row["selected_at"]),
                published_at=datetime.fromisoformat(row["published_at"])
                if row["published_at"] else None,
            )
            for row in database.comic_usage_rows()
        )
        selector = ComicStockSelector()
        for candidate in candidates:
            result = selector.select(
                item_code=candidate.product.item_code,
                search_keyword=candidate.search_keyword,
                category=candidate.search_keyword,
                topic_tag=candidate.topic_tag,
                post_type=candidate.template_variant,
                posting_date=now.date(),
                usages=usages,
            )
            print("-")
            print(f"item_code: {candidate.product.item_code}")
            print(f"keyword: {candidate.search_keyword}")
            print(f"category: {candidate.search_keyword}")
            print(f"topic_tag: {candidate.topic_tag}")
            print(f"comic_id: {result.comic_id or 'NO_COMIC'}")
            print(f"file: {result.file_path.name if result.file_path else 'N/A'}")
            print(f"selection_score: {result.selection_score:.0f}")
            print(f"selection_reason: {result.selection_reason}")
            print(f"last_used_at: {result.last_used_at.isoformat() if result.last_used_at else 'N/A'}")
            print(f"cooldown_status: {result.cooldown_status}")
            print(f"assigned_media_variant: {result.assigned_media_variant}")
            print(f"delivered_media_variant: {result.delivered_media_variant}")
            print(f"publishing_enabled: {str(COMIC_STOCK_PUBLISHING_ENABLED).lower()}")
        after = database.connection.execute("SELECT COUNT(*) FROM comic_usage").fetchone()[0]
        if before != after:
            raise RuntimeError("Preview unexpectedly changed comic usage")
    print(f"COMIC_STOCK_ENABLED={str(COMIC_STOCK_ENABLED).lower()}")
    print(f"COMIC_STOCK_PUBLISHING_ENABLED={str(COMIC_STOCK_PUBLISHING_ENABLED).lower()}")
    print(f"media_hosting_status={COMIC_MEDIA_HOSTING_STATUS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
