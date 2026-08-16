#!/usr/bin/env python3
"""Preview eligible Threads posts without contacting the Threads API."""

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import ConfigurationError, load_settings  # noqa: E402
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402
from app.publishers.threads import find_publishable_candidates  # noqa: E402


def main() -> int:
    try:
        settings = load_settings()
    except ConfigurationError as error:
        print(str(error), file=sys.stderr)
        return 1

    with Database(settings.database_path) as database:
        initialize_database(database)
        candidates = find_publishable_candidates(database)

    if not candidates:
        print("投稿可能な候補はありません。")
        return 0

    print(f"投稿可能候補: {len(candidates)}件（previewのみ・投稿は実行しません）")
    for index, candidate in enumerate(candidates, start=1):
        print("\n" + "=" * 72)
        print(f"候補 {index}")
        print(f"item_code: {candidate.product.item_code}")
        print(f"deal_score: {candidate.deal_score:.2f}")
        price = candidate.product.item_price
        print(f"price: {price:,}円" if price is not None else "price: N/A")
        print(f"投稿可否理由: {candidate.reason}")
        print(f"文字数: {len(candidate.text)}")
        print("-" * 72)
        print(candidate.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
