#!/usr/bin/env python3
"""Collect Rakuten products, persist them, and display the top deal scores."""

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple


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
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402
from app.models import Product  # noqa: E402
from app.rakuten_client import RakutenAPIError, RakutenClient  # noqa: E402
from app.scoring import calculate_deal_score  # noqa: E402


LOGGER = logging.getLogger("rakuten_collector")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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


def load_search_terms(path: Path) -> List[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Search terms configuration could not be loaded") from error
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("Search terms configuration must be a non-empty JSON array")
    terms = [term.strip() for term in payload if isinstance(term, str) and term.strip()]
    if len(terms) != len(payload):
        raise RuntimeError("Every search term must be a non-empty string")
    return terms


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
    scored_products: Sequence[Tuple[float, str, Product]],
    settings: Settings,
) -> None:
    print("\nDeal Score 上位5件")
    for score, keyword, product in sorted(
        scored_products, key=lambda entry: entry[0], reverse=True
    )[:5]:
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

    scored_products: List[Tuple[float, str, Product]] = []
    baseline_prices: Dict[str, Optional[int]] = {}
    baseline_history_counts: Dict[str, int] = {}
    had_error = False

    with Database(settings.database_path) as database, RakutenClient(settings) as client:
        initialize_database(database)
        for keyword in search_terms:
            collection_id = database.start_collection(keyword, utc_now())
            LOGGER.info("Collection started for keyword=%s", keyword)
            try:
                products = client.search(keyword, hits=30)
                for product in products:
                    if product.item_code not in baseline_prices:
                        baseline_prices[product.item_code] = database.previous_price(
                            product.item_code
                        )
                        baseline_history_counts[
                            product.item_code
                        ] = database.price_history_count(product.item_code)
                    previous_price = baseline_prices[product.item_code]
                    score = calculate_deal_score(
                        product,
                        previous_price,
                        price_history_count=baseline_history_counts[product.item_code],
                    )
                    scored_products.append((score, keyword, product))
                database.save_products(products)
                database.finish_collection(
                    collection_id,
                    utc_now(),
                    len(products),
                    "success",
                )
                LOGGER.info(
                    "Collection completed for keyword=%s item_count=%d",
                    keyword,
                    len(products),
                )
            except RakutenAPIError as error:
                had_error = True
                database.finish_collection(collection_id, utc_now(), 0, "failed")
                print(f"keyword={keyword}: {error}", file=sys.stderr)
                LOGGER.error("Collection failed for keyword=%s: %s", keyword, error)
            except Exception:
                had_error = True
                database.finish_collection(collection_id, utc_now(), 0, "failed")
                print(f"keyword={keyword}: unexpected collection error", file=sys.stderr)
                LOGGER.error("Collection failed for keyword=%s: unexpected error", keyword)

    display_top_results(scored_products, settings)
    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
