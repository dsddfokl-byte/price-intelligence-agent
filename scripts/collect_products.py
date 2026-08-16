#!/usr/bin/env python3
"""Collect Rakuten products, persist them, and display the top deal scores."""

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import (  # noqa: E402
    ConfigurationError,
    LOG_PATH,
    SEARCH_TERMS_PATH,
    Settings,
    load_settings,
)
from app.collector import (  # noqa: E402
    ScoredProduct,
    collect_products,
    load_search_terms,
)
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402
from app.rakuten_client import RakutenAPIError, RakutenClient  # noqa: E402


LOGGER = logging.getLogger("rakuten_collector")


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.propagate = False


def redact(value: object, settings: Settings) -> str:
    text = str(value)
    for secret in (
        settings.application_id,
        settings.access_key,
        settings.affiliate_id,
    ):
        text = text.replace(secret, "[REDACTED]")
    return text


def display_top_results(
    scored_products: Sequence[ScoredProduct],
    settings: Settings,
) -> None:
    print("\nDeal Score 上位5件")
    for entry in sorted(
        scored_products, key=lambda entry: entry.deal_score, reverse=True
    )[:5]:
        score = entry.deal_score
        keyword = entry.keyword
        product = entry.product
        print("-")
        print(f"keyword: {redact(keyword, settings)}")
        print(f"item_name: {redact(product.item_name or 'N/A', settings)}")
        price = f"{product.item_price}円" if product.item_price is not None else "N/A"
        print(f"price: {price}")
        print(
            "affiliate_rate: "
            + (str(product.affiliate_rate) if product.affiliate_rate is not None else "N/A")
        )
        print(
            "review_average: "
            + (str(product.review_average) if product.review_average is not None else "N/A")
        )
        print(f"deal_score: {score:.2f}")


def main() -> int:
    configure_logging()
    try:
        settings = load_settings()
        search_terms = load_search_terms(SEARCH_TERMS_PATH)
    except (ConfigurationError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        LOGGER.error("Collector configuration failed")
        return 1

    with Database(settings.database_path) as database, RakutenClient(settings) as client:
        initialize_database(database)
        try:
            result = collect_products(database, client, search_terms, LOGGER, hits=30)
        except RakutenAPIError as error:
            print(str(error), file=sys.stderr)
            LOGGER.error("Collection failed: %s", error)
            return 1
        except Exception:
            print("Unexpected collection error", file=sys.stderr)
            LOGGER.error("Collection failed: unexpected error")
            return 1

    display_top_results(result.scored_products, settings)
    return 0


if __name__ == "__main__":
    sys.exit(main())
