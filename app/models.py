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
    point_rate: Optional[int] = None
    point_rate_start_time: Optional[str] = None
    point_rate_end_time: Optional[str] = None
    postage_flag: Optional[int] = None
    sale_start_time: Optional[str] = None
    sale_end_time: Optional[str] = None
