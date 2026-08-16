"""Safe Threads publishing and post-candidate helpers."""

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from app.config import (
    REQUEST_TIMEOUT,
    THREADS_API_BASE_URL,
    THREADS_PUBLISHING,
    THREADS_USERNAME,
    ThreadsPublishingConfig,
)
from app.database import Database
from app.models import Product
from app.publishers.title_formatter import shorten_product_title
from app.scoring import calculate_deal_score


LOGGER = logging.getLogger("threads_publisher")


class ThreadsAPIError(RuntimeError):
    """A Threads API failure with a credential-safe message."""

    def __init__(
        self,
        message: str,
        *,
        stage: str,
        status_code: Optional[int] = None,
        code: Optional[int] = None,
        error_subcode: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.status_code = status_code
        self.code = code
        self.error_subcode = error_subcode


class ThreadsPostError(RuntimeError):
    """A safe post generation or eligibility error."""


@dataclass(frozen=True)
class ThreadsCandidate:
    product: Product
    deal_score: float
    text: str
    text_hash: str
    reason: str


class ThreadsPublisher:
    def __init__(
        self,
        access_token: str,
        username: str = THREADS_USERNAME,
        api_base_url: str = THREADS_API_BASE_URL,
        timeout: int = REQUEST_TIMEOUT,
    ) -> None:
        if not access_token:
            raise ValueError("Threads access token must not be empty")
        self._access_token = access_token
        self.username = username
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {access_token}"})
        self._user_id: Optional[str] = None

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "ThreadsPublisher":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _redact(self, value: Any) -> str:
        return str(value).replace(self._access_token, "[REDACTED]").replace(
            "\r", " "
        ).replace("\n", " ")

    def _api_error(self, response: requests.Response, stage: str) -> ThreadsAPIError:
        fields: List[str] = []
        code: Optional[int] = None
        error_subcode: Optional[int] = None
        try:
            payload = response.json()
        except (requests.exceptions.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                code = error.get("code") if isinstance(error.get("code"), int) else None
                error_subcode = (
                    error.get("error_subcode")
                    if isinstance(error.get("error_subcode"), int)
                    else None
                )
                for name in ("message", "type", "code", "error_subcode"):
                    if error.get(name) is not None:
                        fields.append(f"{name}={self._redact(error[name])}")
        detail = ", ".join(fields) if fields else "no safe error details"
        return ThreadsAPIError(
            f"Threads {stage} API failed with HTTP {response.status_code}: {detail}",
            stage=stage,
            status_code=response.status_code,
            code=code,
            error_subcode=error_subcode,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        stage: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        for attempt in range(3):
            try:
                response = self.session.request(
                    method,
                    f"{self.api_base_url}{path}",
                    params=params,
                    data=data,
                    timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise ThreadsAPIError(
                        f"Threads {stage} API failed after 3 attempts due to a temporary network error",
                        stage=stage,
                    ) from None
                time.sleep(2**attempt)
                continue
            except requests.RequestException:
                raise ThreadsAPIError(
                    f"Threads {stage} API request failed",
                    stage=stage,
                ) from None

            if 200 <= response.status_code < 300:
                try:
                    payload = response.json()
                except (requests.exceptions.JSONDecodeError, ValueError):
                    raise ThreadsAPIError(
                        f"Threads {stage} API returned invalid JSON",
                        stage=stage,
                    ) from None
                if not isinstance(payload, dict):
                    raise ThreadsAPIError(
                        f"Threads {stage} API returned an unexpected JSON structure",
                        stage=stage,
                    )
                return payload

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
            raise self._api_error(response, stage)

        raise ThreadsAPIError(
            f"Threads {stage} API request failed after 3 attempts",
            stage=stage,
        )

    def get_user_id(self) -> str:
        if self._user_id is not None:
            return self._user_id
        payload = self._request(
            "GET",
            "/me",
            stage="user lookup",
            params={"fields": "id,username"},
        )
        user_id = payload.get("id")
        username = payload.get("username")
        if not user_id:
            raise ThreadsAPIError(
                "Threads user lookup API response did not include a user id",
                stage="user lookup",
            )
        if username != self.username:
            raise ThreadsAPIError(
                "Threads user lookup API returned an unexpected username",
                stage="user lookup",
            )
        self._user_id = str(user_id)
        return self._user_id

    def create_text_container(self, text: str) -> str:
        if not text or len(text) > THREADS_PUBLISHING.maximum_text_length:
            raise ThreadsPostError("Threads text must contain 1 to 500 characters")
        user_id = self.get_user_id()
        payload = self._request(
            "POST",
            f"/{user_id}/threads",
            stage="create",
            params={"media_type": "TEXT", "text": text},
        )
        creation_id = payload.get("id")
        if not creation_id:
            raise ThreadsAPIError(
                "Threads create API response did not include a creation id",
                stage="create",
            )
        safe_creation_id = str(creation_id)
        LOGGER.debug("Threads create succeeded creation_id=%s", safe_creation_id)
        return safe_creation_id

    def publish_container(self, creation_id: str) -> str:
        if not creation_id:
            raise ValueError("creation_id must not be empty")
        user_id = self.get_user_id()
        retry_delays = (0, 2, 4, 8)
        for attempt, delay in enumerate(retry_delays):
            if delay:
                time.sleep(delay)
            try:
                payload = self._request(
                    "POST",
                    f"/{user_id}/threads_publish",
                    stage="publish",
                    params={"creation_id": creation_id},
                )
            except ThreadsAPIError as error:
                container_not_ready = (
                    error.code == 24 and error.error_subcode == 4279009
                )
                if container_not_ready and attempt < len(retry_delays) - 1:
                    LOGGER.warning(
                        "Threads publish container not ready; retry=%d delay=%ds",
                        attempt + 1,
                        retry_delays[attempt + 1],
                    )
                    continue
                raise
            post_id = payload.get("id")
            if not post_id:
                raise ThreadsAPIError(
                    "Threads publish API response did not include a post id",
                    stage="publish",
                )
            return str(post_id)
        raise ThreadsAPIError(
            "Threads publish API failed after container readiness retries",
            stage="publish",
        )

    def publish_text(self, text: str) -> str:
        creation_id = self.create_text_container(text)
        time.sleep(2)
        return self.publish_container(creation_id)


def _product_from_row(row: Any) -> Product:
    return Product(
        item_code=row["item_code"],
        item_name=row["item_name"],
        item_price=row["latest_price"],
        shop_code=row["shop_code"],
        shop_name=row["shop_name"],
        item_url=row["item_url"],
        affiliate_url=row["affiliate_url"],
        review_average=row["review_average"],
        review_count=row["review_count"],
        affiliate_rate=row["affiliate_rate"],
        availability=row["availability"],
        fetched_at=row["last_seen_at"],
    )


def post_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generate_post_text(
    product: Product,
    deal_score: float,
    maximum_length: int = THREADS_PUBLISHING.maximum_text_length,
) -> str:
    if not product.affiliate_url:
        raise ThreadsPostError("Affiliate URL is required")
    title = shorten_product_title(product.item_name)
    price = f"{product.item_price:,}円" if product.item_price is not None else "価格情報なし"
    review_average = (
        f"{product.review_average:g}"
        if product.review_average is not None
        else "評価なし"
    )
    review_count = f"{product.review_count:,}" if product.review_count is not None else "0"

    def render(display_title: str) -> str:
        return (
            "🔥 買い時レーダー\n\n"
            f"{display_title}\n\n"
            f"現在価格：{price}\n"
            f"レビュー：★{review_average}（{review_count}件）\n"
            f"Deal Score：{deal_score:.0f}/100\n\n"
            "楽天市場で確認\n"
            f"{product.affiliate_url}\n\n"
            "※本投稿にはアフィリエイトリンクが含まれます。"
        )

    display_title = title
    text = render(display_title)
    if len(text) > maximum_length:
        excess = len(text) - maximum_length
        keep = len(display_title) - excess
        if keep < 2:
            raise ThreadsPostError("Affiliate URL is too long for a safe Threads post")
        display_title = display_title[: keep - 1].rstrip() + "…"
        text = render(display_title)
    if len(text) > maximum_length:
        raise ThreadsPostError("Generated Threads post exceeds 500 characters")
    return text


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_day_start(now: datetime) -> datetime:
    return now.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def evaluate_product(
    database: Database,
    row: Any,
    config: ThreadsPublishingConfig = THREADS_PUBLISHING,
    now: Optional[datetime] = None,
) -> Tuple[Optional[ThreadsCandidate], str]:
    current_time = now or utc_now()
    product = _product_from_row(row)
    previous_price = row["previous_price"]
    deal_score = calculate_deal_score(
        product,
        previous_price,
        price_history_count=row["price_history_count"],
    )

    if deal_score < config.minimum_deal_score:
        return None, f"Deal Scoreが{config.minimum_deal_score:.0f}未満"
    if product.review_count is None or product.review_count < config.minimum_review_count:
        return None, f"レビュー件数が{config.minimum_review_count}件未満"
    if not product.affiliate_url:
        return None, "affiliate_urlが存在しない"

    try:
        text = generate_post_text(product, deal_score, config.maximum_text_length)
    except ThreadsPostError as error:
        return None, str(error)
    digest = post_text_hash(text)

    override_reason = ""
    latest_post = database.latest_published_threads_post(product.item_code)
    if latest_post is not None:
        posted_at = datetime.fromisoformat(latest_post["posted_at"])
        within_item_cooldown = posted_at >= current_time - timedelta(
            days=config.item_cooldown_days
        )
        previous_post_price = latest_post["price"]
        price_drop_override = (
            within_item_cooldown
            and previous_post_price is not None
            and previous_post_price > 0
            and product.item_price is not None
            and product.item_price
            <= previous_post_price * (1.0 - config.price_drop_override)
        )
        if within_item_cooldown and not price_drop_override:
            return None, f"同一商品を{config.item_cooldown_days}日以内に投稿済み"
        if price_drop_override:
            override_reason = "（前回投稿価格から10%以上値下がり）"

    hash_since = (
        current_time - timedelta(days=config.text_cooldown_days)
    ).isoformat()
    if database.has_published_text_hash_since(digest, hash_since):
        return None, f"同一投稿文を{config.text_cooldown_days}日以内に投稿済み"

    reason = "投稿条件を満たしています" + override_reason
    return ThreadsCandidate(product, deal_score, text, digest, reason), reason


def find_publishable_candidates(
    database: Database,
    config: ThreadsPublishingConfig = THREADS_PUBLISHING,
    now: Optional[datetime] = None,
) -> List[ThreadsCandidate]:
    current_time = now or utc_now()
    posted_today = database.published_threads_count_since(
        utc_day_start(current_time).isoformat()
    )
    remaining = max(0, config.daily_post_limit - posted_today)
    limit = min(config.candidate_limit, remaining)
    if limit == 0:
        return []

    candidates: List[ThreadsCandidate] = []
    for row in database.products_for_threads():
        candidate, _ = evaluate_product(database, row, config, current_time)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda candidate: candidate.deal_score, reverse=True)
    return candidates[:limit]
