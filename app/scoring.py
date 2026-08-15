"""Initial, configurable deal scoring without AI-based decisions."""

import math
from dataclasses import dataclass
from typing import Optional

from app.models import Product


@dataclass(frozen=True)
class ScoreWeights:
    affiliate_rate: float = 25.0
    review_average: float = 25.0
    review_count: float = 20.0
    price_drop: float = 30.0


DEFAULT_WEIGHTS = ScoreWeights()


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def calculate_deal_score(
    product: Product,
    previous_price: Optional[int] = None,
    weights: ScoreWeights = DEFAULT_WEIGHTS,
) -> float:
    """Return a 0-100 score, omitting price-drop weight when history is absent."""
    components = []
    if product.affiliate_rate is not None:
        components.append((_clamp(product.affiliate_rate / 10.0), weights.affiliate_rate))
    if product.review_average is not None:
        components.append((_clamp(product.review_average / 5.0), weights.review_average))
    if product.review_count is not None:
        review_score = math.log1p(max(0, product.review_count)) / math.log1p(1000)
        components.append((_clamp(review_score), weights.review_count))
    if (
        previous_price is not None
        and previous_price > 0
        and product.item_price is not None
    ):
        drop_ratio = (previous_price - product.item_price) / previous_price
        components.append((_clamp(drop_ratio / 0.30), weights.price_drop))

    total_weight = sum(weight for _, weight in components)
    if total_weight <= 0:
        return 0.0
    score = sum(value * weight for value, weight in components) / total_weight * 100.0
    return round(_clamp(score / 100.0) * 100.0, 2)
