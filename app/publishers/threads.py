"""Safe Threads publishing and post-candidate helpers."""

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

from app.config import (
    REQUEST_TIMEOUT,
    THREADS_API_BASE_URL,
    THREADS_PUBLISHING,
    THREADS_TOPIC_TAGS,
    THREADS_USERNAME,
    ThreadsPublishingConfig,
)
from app.database import Database
from app.models import Product
from app.scoring import calculate_deal_score
from app.thread_content import (
    OWNER_VALUE,
    PRICE_CONTROL,
    OwnerTip,
    assign_variant,
    build_content_trigger,
    generate_experiment_text,
    load_owner_tips,
    order_tips,
)


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
    search_keyword: str
    topic_tag: str
    template_variant: str
    tip_id: Optional[str]
    content_trigger: Optional[str]


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

    def _create_text_container_once(
        self, text: str, topic_tag: Optional[str]
    ) -> str:
        user_id = self.get_user_id()
        params = {"media_type": "TEXT", "text": text}
        if topic_tag:
            params["topic_tag"] = topic_tag
        payload = self._request(
            "POST",
            f"/{user_id}/threads",
            stage="create",
            params=params,
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

    def create_text_container(
        self, text: str, topic_tag: Optional[str] = None
    ) -> str:
        if not text or len(text) > THREADS_PUBLISHING.maximum_text_length:
            raise ThreadsPostError("Threads text must contain 1 to 500 characters")
        try:
            return self._create_text_container_once(text, topic_tag)
        except ThreadsAPIError as error:
            topic_fallback = (
                topic_tag is not None
                and error.status_code is not None
                and 400 <= error.status_code < 500
                and error.status_code != 429
            )
            if not topic_fallback:
                raise
            LOGGER.warning(
                "Threads create rejected topic_tag; retrying once without topic_tag"
            )
            return self._create_text_container_once(text, None)

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

    def publish_text(self, text: str, topic_tag: Optional[str] = None) -> str:
        creation_id = self.create_text_container(text, topic_tag)
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
        point_rate=row["point_rate"],
        point_rate_start_time=row["point_rate_start_time"],
        point_rate_end_time=row["point_rate_end_time"],
        postage_flag=row["postage_flag"],
        sale_start_time=row["sale_start_time"],
        sale_end_time=row["sale_end_time"],
    )


def post_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generate_post_text(
    product: Product,
    deal_score: float,
    maximum_length: int = THREADS_PUBLISHING.maximum_text_length,
    requires_pr_label: bool = False,
) -> str:
    try:
        return generate_experiment_text(
            product,
            deal_score,
            "ペットシーツ",
            PRICE_CONTROL,
            None,
            None,
            maximum_length,
            requires_pr_label,
        )
    except ValueError as error:
        raise ThreadsPostError(str(error)) from None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def daily_period_start(
    now: datetime,
    timezone_name: str = THREADS_PUBLISHING.daily_timezone,
) -> datetime:
    local_midnight = now.astimezone(ZoneInfo(timezone_name)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return local_midnight.astimezone(timezone.utc)


def _build_candidate_content(
    database: Database,
    product: Product,
    search_keyword: str,
    previous_price: Optional[int],
    deal_score: float,
    current_time: datetime,
    reason: str,
    config: ThreadsPublishingConfig,
) -> ThreadsCandidate:
    variant = assign_variant(product.item_code, current_time)
    selected_tip: Optional[OwnerTip] = None
    if variant == OWNER_VALUE:
        tip_since = (current_time - timedelta(days=30)).isoformat()
        for tip in order_tips(
            load_owner_tips(), search_keyword, product.item_code, current_time
        ):
            if not database.has_published_tip_since(tip.tip_id, tip_since):
                selected_tip = tip
                break
        if selected_tip is None:
            variant = PRICE_CONTROL

    content_trigger = build_content_trigger(product, previous_price)
    text = generate_experiment_text(
        product,
        deal_score,
        search_keyword,
        variant,
        selected_tip,
        content_trigger,
        config.maximum_text_length,
    )
    return ThreadsCandidate(
        product=product,
        deal_score=deal_score,
        text=text,
        text_hash=post_text_hash(text),
        reason=reason,
        search_keyword=search_keyword,
        topic_tag=THREADS_TOPIC_TAGS[search_keyword],
        template_variant=variant,
        tip_id=selected_tip.tip_id if selected_tip else None,
        content_trigger=content_trigger,
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
    search_keyword = row["search_keyword"]
    if search_keyword not in THREADS_TOPIC_TAGS:
        return None, "検索カテゴリーにtopic_tag設定がありません"
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

    try:
        candidate = _build_candidate_content(
            database,
            product,
            search_keyword,
            previous_price,
            deal_score,
            current_time,
            "投稿条件を満たしています" + override_reason,
            config,
        )
    except ValueError as error:
        return None, str(error)

    hash_since = (
        current_time - timedelta(days=config.text_cooldown_days)
    ).isoformat()
    if database.has_published_text_hash_since(candidate.text_hash, hash_since):
        return None, f"同一投稿文を{config.text_cooldown_days}日以内に投稿済み"

    return candidate, candidate.reason


def find_preview_candidates(
    database: Database,
    config: ThreadsPublishingConfig = THREADS_PUBLISHING,
    now: Optional[datetime] = None,
) -> List[ThreadsCandidate]:
    """Return content samples without changing production eligibility rules."""
    current_time = now or utc_now()
    candidates: List[ThreadsCandidate] = []
    template_samples: List[ThreadsCandidate] = []
    for row in database.products_for_threads():
        candidate, _ = evaluate_product(database, row, config, current_time)
        if candidate is not None:
            candidates.append(candidate)
        if row["search_keyword"] in THREADS_TOPIC_TAGS:
            product = _product_from_row(row)
            deal_score = calculate_deal_score(
                product,
                row["previous_price"],
                price_history_count=row["price_history_count"],
            )
            try:
                template_samples.append(
                    _build_candidate_content(
                        database,
                        product,
                        row["search_keyword"],
                        row["previous_price"],
                        deal_score,
                        current_time,
                        "実験テンプレートpreview（投稿可否判定とは別）",
                        config,
                    )
                )
            except ValueError:
                pass
    candidates.sort(key=lambda candidate: candidate.deal_score, reverse=True)
    if len(candidates) >= 2 and {item.template_variant for item in candidates} == {
        PRICE_CONTROL,
        OWNER_VALUE,
    }:
        return candidates[: config.candidate_limit]
    template_samples.sort(key=lambda candidate: candidate.deal_score, reverse=True)
    selected: List[ThreadsCandidate] = []
    for variant in (PRICE_CONTROL, OWNER_VALUE):
        match = next(
            (item for item in template_samples if item.template_variant == variant),
            None,
        )
        if match is not None:
            selected.append(match)
    for item in template_samples:
        if item not in selected and len(selected) < config.candidate_limit:
            selected.append(item)
    return selected


def find_publishable_candidates(
    database: Database,
    config: ThreadsPublishingConfig = THREADS_PUBLISHING,
    now: Optional[datetime] = None,
) -> List[ThreadsCandidate]:
    current_time = now or utc_now()
    posted_today = database.published_threads_count_since(
        daily_period_start(current_time, config.daily_timezone).isoformat()
    )
    remaining = max(0, config.daily_post_limit - posted_today)
    limit = min(config.candidate_limit, remaining)
    if limit == 0:
        return []

    return find_eligible_candidates(database, config, current_time)[:limit]


def find_eligible_candidates(
    database: Database,
    config: ThreadsPublishingConfig = THREADS_PUBLISHING,
    now: Optional[datetime] = None,
) -> List[ThreadsCandidate]:
    """Apply production eligibility once and return the complete sorted pool."""
    current_time = now or utc_now()
    candidates: List[ThreadsCandidate] = []
    for row in database.products_for_threads():
        candidate, _ = evaluate_product(database, row, config, current_time)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda candidate: candidate.deal_score, reverse=True)
    return candidates
