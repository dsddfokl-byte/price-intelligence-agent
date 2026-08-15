"""Domain models used by the collection pipeline."""

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Product:
    item_code: str
    item_name: Optional[str]
    item_price: Optional[int]
    shop_code: Optional[str]
    shop_name: Optional[str]
    item_url: Optional[str]
    affiliate_url: Optional[str]
    review_average: Optional[float]
    review_count: Optional[int]
    affiliate_rate: Optional[float]
    availability: Optional[int]
    fetched_at: str
