"""Non-critical comic assignment and delivery for one production cycle."""

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Sequence

from app.comics.media_hosting import (
    GitHubPagesComicMediaHostingProvider,
    HostedComicAsset,
    MediaHostingError,
    configured_provider,
)
from app.comics.media_publisher import ComicThreadsPublisher, build_comic_alt_text
from app.comics.stock_selector import (
    COMIC,
    NO_COMIC,
    ComicSelection,
    ComicStockSelector,
    ComicUsageRecord,
)
from app.config import COMIC_STOCK_PUBLISHING_ENABLED
from app.publishers.threads import ThreadsAPIError, ThreadsCandidate, ThreadsPostError


LOGGER = logging.getLogger("affiliate_automation")
COMIC_SELECTION_FAILED = "COMIC_SELECTION_FAILED"
COMIC_MEDIA_FAILED = "COMIC_MEDIA_FAILED"


@dataclass(frozen=True)
class ComicPlan:
    selection: ComicSelection
    theme: Optional[str] = None

    @property
    def assigned_media_variant(self) -> str:
        return self.selection.assigned_media_variant


@dataclass(frozen=True)
class ComicPublishOutcome:
    post_id: str
    assigned_media_variant: str
    delivered_media_variant: str
    selection: ComicSelection
    hosted: Optional[HostedComicAsset] = None
    fallback_reason: Optional[str] = None
    permalink: Optional[str] = None


def build_comic_plan(
    candidate: ThreadsCandidate,
    *,
    now: datetime,
    usages: Sequence[ComicUsageRecord],
    selector: Optional[ComicStockSelector] = None,
) -> ComicPlan:
    stock_selector = selector or ComicStockSelector()
    selection = stock_selector.select(
        item_code=candidate.product.item_code,
        search_keyword=candidate.search_keyword,
        category=candidate.search_keyword,
        topic_tag=candidate.topic_tag,
        post_type=candidate.template_variant,
        posting_date=now.date(),
        usages=usages,
    )
    theme = next(
        (
            asset.theme
            for asset in stock_selector.validation.valid_assets
            if asset.comic_id == selection.comic_id
        ),
        None,
    )
    return ComicPlan(selection, theme)


def _text_outcome(
    publisher: ComicThreadsPublisher,
    candidate: ThreadsCandidate,
    plan: ComicPlan,
    fallback_reason: Optional[str] = None,
) -> ComicPublishOutcome:
    post_id = publisher.publish_text(candidate.text, candidate.topic_tag)
    permalink = None
    try:
        permalink = publisher.get_post_details(post_id).get("permalink")
    except ThreadsAPIError:
        LOGGER.warning("Threads permalink lookup failed post_id=%s", post_id)
    return ComicPublishOutcome(
        post_id=post_id,
        assigned_media_variant=plan.assigned_media_variant,
        delivered_media_variant=NO_COMIC,
        selection=plan.selection,
        fallback_reason=fallback_reason,
        permalink=str(permalink) if permalink else None,
    )


def publish_with_comic_plan(
    publisher: ComicThreadsPublisher,
    candidate: ThreadsCandidate,
    plan: ComicPlan,
    *,
    provider: Optional[GitHubPagesComicMediaHostingProvider] = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> ComicPublishOutcome:
    """Publish at most one post, falling back before an IMAGE publish attempt."""
    if not COMIC_STOCK_PUBLISHING_ENABLED or plan.assigned_media_variant != COMIC:
        return _text_outcome(publisher, candidate, plan)
    selection = plan.selection
    if not selection.comic_id or not selection.file_path:
        LOGGER.warning("COMIC_FALLBACK_TO_TEXT reason=%s", COMIC_SELECTION_FAILED)
        return _text_outcome(
            publisher, candidate, plan, COMIC_SELECTION_FAILED
        )
    try:
        media_provider = provider or configured_provider()
        if not isinstance(media_provider, GitHubPagesComicMediaHostingProvider):
            raise MediaHostingError("github_pages provider is required")
        hosted = media_provider.resolve_asset(
            selection.comic_id, validate_remote=True
        )
        alt_text = build_comic_alt_text(plan.theme or "PET_LIFE", candidate.search_keyword)
        creation_id = publisher.create_image_container(
            candidate.text, hosted.public_url, alt_text, candidate.topic_tag
        )
        status = "IN_PROGRESS"
        for delay in (0, 2, 4, 8, 16):
            if delay:
                sleeper(delay)
            status = publisher.get_container_status(creation_id)
            if status != "IN_PROGRESS":
                break
        if status != "FINISHED":
            raise ThreadsPostError(f"Comic container status={status}")
    except (MediaHostingError, ThreadsPostError) as error:
        LOGGER.warning(
            "COMIC_FALLBACK_TO_TEXT reason=%s error_type=%s",
            COMIC_MEDIA_FAILED,
            type(error).__name__,
        )
        return _text_outcome(publisher, candidate, plan, COMIC_MEDIA_FAILED)
    except ThreadsAPIError as error:
        if error.stage == "publish":
            raise
        LOGGER.warning(
            "COMIC_FALLBACK_TO_TEXT reason=%s error_type=%s",
            COMIC_MEDIA_FAILED,
            type(error).__name__,
        )
        return _text_outcome(publisher, candidate, plan, COMIC_MEDIA_FAILED)

    # No fallback is allowed beyond this point: the publish result could be ambiguous.
    post_id = publisher.publish_finished_container_once(creation_id, status)
    permalink = None
    try:
        permalink = publisher.get_post_details(post_id).get("permalink")
    except ThreadsAPIError:
        LOGGER.warning("Threads permalink lookup failed post_id=%s", post_id)
    return ComicPublishOutcome(
        post_id=post_id,
        assigned_media_variant=COMIC,
        delivered_media_variant=COMIC,
        selection=selection,
        hosted=hosted,
        permalink=str(permalink) if permalink else None,
    )
