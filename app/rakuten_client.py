"""Safe client for the Rakuten Ichiba Item Search API."""

import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

from app.config import Settings
from app.models import Product


ELEMENTS = (
    "itemCode,itemName,itemPrice,shopCode,shopName,itemUrl,affiliateUrl,"
    "reviewAverage,reviewCount,affiliateRate,availability,pointRate,"
    "pointRateStartTime,pointRateEndTime,postageFlag,startTime,endTime"
)


class RakutenAPIError(RuntimeError):
    """An API failure with a credential-safe message."""


class RakutenClient:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"accessKey": settings.access_key})

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "RakutenClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _redact(self, value: Any) -> str:
        text = str(value).replace("\r", " ").replace("\n", " ")
        for secret in (
            self.settings.application_id,
            self.settings.access_key,
            self.settings.affiliate_id,
        ):
            text = text.replace(secret, "[REDACTED]")
        return text

    def _sanitize_url(self, value: Any) -> Optional[str]:
        """Remove query parameters containing credentials before persistence."""
        if value is None:
            return None
        url = str(value)
        secrets = (
            self.settings.application_id,
            self.settings.access_key,
            self.settings.affiliate_id,
        )
        parsed = urllib.parse.urlsplit(url)
        safe_query = [
            (key, item_value)
            for key, item_value in urllib.parse.parse_qsl(
                parsed.query, keep_blank_values=True
            )
            if not any(
                secret in key or secret in item_value for secret in secrets
            )
        ]
        return urllib.parse.urlunsplit(
            parsed._replace(query=urllib.parse.urlencode(safe_query, doseq=True))
        )

    def _sanitize_optional_text(self, value: Any) -> Optional[str]:
        return self._redact(value) if value is not None else None

    def _safe_api_error(self, response: requests.Response) -> str:
        fields: List[str] = []
        try:
            payload = response.json()
        except (requests.exceptions.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            for name in ("error", "error_description"):
                value = payload.get(name)
                if value is not None:
                    fields.append(f"{name}={self._redact(value)}")
        detail = ", ".join(fields) if fields else "no safe error details"
        return f"Rakuten API returned HTTP {response.status_code}: {detail}"

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _to_product(self, item: Dict[str, Any], fetched_at: str) -> Optional[Product]:
        item_code = item.get("itemCode")
        if not item_code:
            return None
        return Product(
            item_code=self._redact(item_code),
            item_name=self._sanitize_optional_text(item.get("itemName")),
            item_price=self._optional_int(item.get("itemPrice")),
            shop_code=self._sanitize_optional_text(item.get("shopCode")),
            shop_name=self._sanitize_optional_text(item.get("shopName")),
            item_url=self._sanitize_url(item.get("itemUrl")),
            affiliate_url=self._sanitize_url(item.get("affiliateUrl")),
            review_average=self._optional_float(item.get("reviewAverage")),
            review_count=self._optional_int(item.get("reviewCount")),
            affiliate_rate=self._optional_float(item.get("affiliateRate")),
            availability=self._optional_int(item.get("availability")),
            fetched_at=fetched_at,
            point_rate=self._optional_int(item.get("pointRate")),
            point_rate_start_time=self._sanitize_optional_text(
                item.get("pointRateStartTime")
            ),
            point_rate_end_time=self._sanitize_optional_text(
                item.get("pointRateEndTime")
            ),
            postage_flag=self._optional_int(item.get("postageFlag")),
            sale_start_time=self._sanitize_optional_text(item.get("startTime")),
            sale_end_time=self._sanitize_optional_text(item.get("endTime")),
        )

    def search(self, keyword: str, hits: int = 30, page: int = 1) -> List[Product]:
        if not keyword.strip():
            raise ValueError("keyword must not be empty")
        if not 1 <= hits <= 30:
            raise ValueError("hits must be between 1 and 30")
        if not 1 <= page <= 100:
            raise ValueError("page must be between 1 and 100")

        params = {
            "applicationId": self.settings.application_id,
            "affiliateId": self.settings.affiliate_id,
            "keyword": keyword,
            "hits": hits,
            "page": page,
            "format": "json",
            "formatVersion": 2,
            "elements": ELEMENTS,
        }

        response: Optional[requests.Response] = None
        for attempt in range(3):
            try:
                response = self.session.get(
                    self.settings.api_url,
                    params=params,
                    timeout=self.settings.timeout,
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise RakutenAPIError(
                        "Rakuten API request failed after 3 attempts due to a temporary network error"
                    ) from None
                time.sleep(2**attempt)
                continue
            except requests.RequestException:
                raise RakutenAPIError("Rakuten API request failed") from None

            if response.status_code == 200:
                break
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
            raise RakutenAPIError(self._safe_api_error(response))

        if response is None or response.status_code != 200:
            raise RakutenAPIError("Rakuten API request failed after 3 attempts")

        try:
            payload = response.json()
        except (requests.exceptions.JSONDecodeError, ValueError):
            raise RakutenAPIError("Rakuten API returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise RakutenAPIError("Rakuten API returned an unexpected JSON structure")

        raw_items = payload.get("Items", payload.get("items", []))
        if not isinstance(raw_items, list):
            raise RakutenAPIError("Rakuten API returned an unexpected items structure")

        fetched_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        products: List[Product] = []
        for raw_item in raw_items:
            if isinstance(raw_item, dict):
                product = self._to_product(raw_item, fetched_at)
                if product is not None:
                    products.append(product)
        return products
