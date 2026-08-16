"""Reusable Rakuten product collection service."""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from app.database import Database
from app.models import Product
from app.rakuten_client import RakutenClient
from app.scoring import calculate_deal_score


@dataclass(frozen=True)
class ScoredProduct:
    keyword: str
    product: Product
    deal_score: float


@dataclass(frozen=True)
class CollectionResult:
    fetched_count: int
    updated_count: int
    scored_products: Sequence[ScoredProduct]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


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


def collect_products(
    database: Database,
    client: RakutenClient,
    search_terms: Sequence[str],
    logger: logging.Logger,
    hits: int = 30,
) -> CollectionResult:
    """Collect every configured term or raise on the first fatal API failure."""
    scored_products: List[ScoredProduct] = []
    baseline_prices: Dict[str, Optional[int]] = {}
    baseline_history_counts: Dict[str, int] = {}
    updated_item_codes: Set[str] = set()
    fetched_count = 0

    for keyword in search_terms:
        collection_id = database.start_collection(keyword, utc_now())
        logger.info("Collection started keyword=%s", keyword)
        try:
            products = client.search(keyword, hits=hits)
            fetched_count += len(products)
            logger.info("Rakuten API fetched keyword=%s item_count=%d", keyword, len(products))
            for product in products:
                if product.item_code not in baseline_prices:
                    baseline_prices[product.item_code] = database.previous_price(
                        product.item_code
                    )
                    baseline_history_counts[
                        product.item_code
                    ] = database.price_history_count(product.item_code)
                score = calculate_deal_score(
                    product,
                    baseline_prices[product.item_code],
                    price_history_count=baseline_history_counts[product.item_code],
                )
                scored_products.append(ScoredProduct(keyword, product, score))
                updated_item_codes.add(product.item_code)
            database.save_products(products)
            database.save_product_keywords(
                keyword,
                (product.item_code for product in products),
                products[0].fetched_at if products else utc_now(),
            )
            database.finish_collection(
                collection_id,
                utc_now(),
                len(products),
                "success",
            )
        except Exception:
            database.finish_collection(collection_id, utc_now(), 0, "failed")
            raise

    return CollectionResult(
        fetched_count=fetched_count,
        updated_count=len(updated_item_codes),
        scored_products=scored_products,
    )
