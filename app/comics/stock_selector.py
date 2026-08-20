"""Deterministic, side-effect-free selection from approved comic stock."""

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional, Sequence

from app.comics.stock_validator import ComicAsset, load_and_validate_manifest
from app.config import (
    COMIC_STOCK_EXPERIMENT_SALT,
    COMIC_STOCK_MANIFEST_PATH,
)


COMIC = "COMIC"
NO_COMIC = "NO_COMIC"


@dataclass(frozen=True)
class ComicUsageRecord:
    comic_id: str
    item_code: str
    category: str
    selected_at: datetime
    published_at: Optional[datetime] = None


@dataclass(frozen=True)
class ComicSelection:
    comic_id: Optional[str]
    file_path: Optional[Path]
    stock_version: str
    selection_score: float
    selection_reason: str
    last_used_at: Optional[datetime]
    cooldown_status: str
    assigned_media_variant: str
    delivered_media_variant: str = NO_COMIC


def assign_media_variant(item_code: str, posting_date: date) -> str:
    key = f"{COMIC_STOCK_EXPERIMENT_SALT}|{item_code}|{posting_date.isoformat()}"
    return COMIC if hashlib.sha256(key.encode()).digest()[0] % 2 == 0 else NO_COMIC


class ComicStockSelector:
    def __init__(self, manifest_path: Path = COMIC_STOCK_MANIFEST_PATH) -> None:
        self.validation = load_and_validate_manifest(manifest_path)

    def select(
        self,
        *,
        item_code: str,
        search_keyword: str,
        category: str,
        topic_tag: str,
        post_type: str,
        posting_date: date,
        usages: Sequence[ComicUsageRecord] = (),
        assigned_media_variant: Optional[str] = None,
    ) -> ComicSelection:
        assigned = assigned_media_variant or assign_media_variant(item_code, posting_date)
        if assigned == NO_COMIC:
            return ComicSelection(None, None, self.validation.stock_version, 0, "NO_COMIC experiment arm", None, "NOT_APPLICABLE", NO_COMIC)
        now = datetime.combine(posting_date, datetime.min.time(), tzinfo=timezone.utc)
        recent_themes = set()
        by_id = {asset.comic_id: asset for asset in self.validation.valid_assets}
        for usage in usages:
            asset = by_id.get(usage.comic_id)
            if asset and usage.selected_at >= now - timedelta(days=30):
                recent_themes.add(asset.theme)
        scored = []
        for asset in self.validation.valid_assets:
            if not asset.enabled:
                continue
            same_product = any(
                usage.comic_id == asset.comic_id and usage.item_code == item_code
                for usage in usages
            )
            last_used = max(
                (usage.selected_at for usage in usages if usage.comic_id == asset.comic_id),
                default=None,
            )
            cooling = bool(last_used and last_used >= now - timedelta(days=asset.reuse_cooldown_days))
            if same_product or cooling:
                continue
            category_score = int(asset.category_scores.get(category, asset.category_scores.get(search_keyword, 0)))
            if category_score <= 0:
                continue
            topic_score = 20 if topic_tag in asset.topic_tags else 0
            diversity = 10 if asset.theme not in recent_themes else 0
            unused = 20 if last_used is None else 10
            score = category_score + topic_score + diversity + unused
            reason = (
                f"category={category_score}, topic={topic_score}, "
                f"theme_diversity={diversity}, recently_unused={unused}, post_type={post_type}"
            )
            tie_key = hashlib.sha256(
                f"{item_code}|{posting_date.isoformat()}|{self.validation.stock_version}|{category}|{asset.comic_id}".encode()
            ).hexdigest()
            scored.append((score, tie_key, asset, last_used, reason))
        if not scored:
            return ComicSelection(None, None, self.validation.stock_version, 0, "No valid non-cooldown candidate", None, "NO_CANDIDATE", COMIC)
        score, _, asset, last_used, reason = max(scored, key=lambda row: (row[0], row[1]))
        return ComicSelection(asset.comic_id, asset.file_path, self.validation.stock_version, score, reason, last_used, "AVAILABLE", COMIC)
