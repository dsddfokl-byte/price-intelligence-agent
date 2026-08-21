"""Deterministic, factual content for the two-variant Threads experiment."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional, Sequence
from zoneinfo import ZoneInfo

from app.config import PET_OWNER_TIPS_PATH, THREADS_PUBLISHING
from app.models import Product
from app.post_text import AFFILIATE_DISCLOSURE, normalize_affiliate_disclosure
from app.publishers.title_formatter import shorten_product_title


PRICE_CONTROL = "PRICE_CONTROL"
OWNER_VALUE = "OWNER_VALUE"
VARIANTS = (PRICE_CONTROL, OWNER_VALUE)


class ContentConfigurationError(RuntimeError):
    """Raised when reviewed owner-tip configuration is invalid."""


@dataclass(frozen=True)
class OwnerTip:
    tip_id: str
    category: str
    text: str
    source_name: Optional[str]
    source_url: Optional[str]


def load_owner_tips(path: Path = PET_OWNER_TIPS_PATH) -> List[OwnerTip]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ContentConfigurationError("Pet owner tips could not be loaded") from error
    if not isinstance(payload, list):
        raise ContentConfigurationError("Pet owner tips must be a JSON array")
    tips: List[OwnerTip] = []
    seen = set()
    for entry in payload:
        if not isinstance(entry, dict) or entry.get("reviewed") is not True:
            continue
        tip_id = entry.get("tip_id")
        category = entry.get("category")
        text = entry.get("text")
        if not all(isinstance(value, str) and value.strip() for value in (tip_id, category, text)):
            raise ContentConfigurationError("Every reviewed pet owner tip needs an id, category, and text")
        if tip_id in seen:
            raise ContentConfigurationError("Pet owner tip ids must be unique")
        seen.add(tip_id)
        tips.append(
            OwnerTip(
                tip_id=tip_id,
                category=category,
                text=text,
                source_name=entry.get("source_name"),
                source_url=entry.get("source_url"),
            )
        )
    if not tips:
        raise ContentConfigurationError("No reviewed pet owner tips are available")
    return tips


def experiment_date(now: datetime) -> str:
    return now.astimezone(ZoneInfo(THREADS_PUBLISHING.daily_timezone)).date().isoformat()


def assign_variant(item_code: str, now: datetime) -> str:
    digest = hashlib.sha256(f"{item_code}|{experiment_date(now)}".encode()).digest()
    return VARIANTS[digest[0] % 2]


def order_tips(
    tips: Sequence[OwnerTip], category: str, item_code: str, now: datetime
) -> List[OwnerTip]:
    matching = [tip for tip in tips if tip.category == category]
    seed = f"{item_code}|{experiment_date(now)}"
    return sorted(
        matching,
        key=lambda tip: hashlib.sha256(f"{seed}|{tip.tip_id}".encode()).digest(),
    )


def build_content_trigger(product: Product, previous_price: Optional[int]) -> Optional[str]:
    if (
        previous_price is not None
        and previous_price > 0
        and product.item_price is not None
        and product.item_price < previous_price
    ):
        decrease = (previous_price - product.item_price) / previous_price * 100
        return f"前回チェックより{decrease:.1f}%下落"
    if product.point_rate is not None and product.point_rate > 1:
        return f"ポイント{product.point_rate}倍"
    if product.postage_flag == 0:
        return "送料込み"
    if product.review_count is not None and product.review_count >= 1000:
        threshold = product.review_count // 1000 * 1000
        return f"レビュー{threshold:,}件以上"
    return None


def _category_style(category: str) -> tuple:
    if category in ("猫 フード", "猫砂"):
        return "🐱", "ねこ"
    if category == "犬 フード":
        return "🐶", "犬"
    return "🐾", "ペット"


def _format_trigger(content_trigger: Optional[str]) -> str:
    if not content_trigger:
        return ""
    if content_trigger.startswith("前回チェックより"):
        icon = "📉"
    elif content_trigger.startswith("ポイント"):
        icon = "🎁"
    elif content_trigger == "送料込み":
        icon = "📦"
    elif content_trigger.startswith("レビュー"):
        icon = "⭐"
    else:
        icon = "💡"
    return f"{icon} {content_trigger}\n\n"


def generate_experiment_text(
    product: Product,
    deal_score: float,
    category: str,
    variant: str,
    tip: Optional[OwnerTip],
    content_trigger: Optional[str],
    maximum_length: int = THREADS_PUBLISHING.maximum_text_length,
    requires_pr_label: bool = False,
) -> str:
    if not product.affiliate_url:
        raise ValueError("Affiliate URL is required")
    if variant not in VARIANTS:
        raise ValueError("Unknown Threads template variant")
    if variant == OWNER_VALUE and tip is None:
        raise ValueError("OWNER_VALUE requires an owner tip")

    emoji, label = _category_style(category)
    title = shorten_product_title(product.item_name)
    price = f"{product.item_price:,}円" if product.item_price is not None else "価格情報なし"
    review = ""
    if product.review_average is not None:
        review = f"★{product.review_average:g}"
        if product.review_count is not None:
            review += f" / {product.review_count:,}件"
    trigger_line = _format_trigger(content_trigger)
    tip_text = tip.text if tip else ""
    pr_label = "【PR】" if requires_pr_label else ""

    def render(display_title: str, utility: str, review_line: str) -> str:
        if variant == PRICE_CONTROL:
            body = (
                f"{pr_label}{emoji} 今日の{label}用品価格チェック\n\n"
                f"{trigger_line}{display_title}\n"
                f"確認時価格：{price}\n\n"
                f"{review_line}\n"
                f"買い時スコア：{deal_score:.1f}\n\n"
            )
        else:
            body = (
                f"{pr_label}{emoji} 30秒{label}メモ\n\n"
                f"{utility}\n\n"
                f"{trigger_line}今日チェックした商品\n"
                f"{display_title}\n\n"
                f"確認時価格：{price}\n"
                f"{review_line}\n\n"
            )
        return normalize_affiliate_disclosure(
            body
            + "楽天市場で確認\n"
            + f"{product.affiliate_url}\n\n"
            + AFFILIATE_DISCLOSURE
        )

    text = render(title, tip_text, review)
    if len(text) > maximum_length and variant == OWNER_VALUE:
        tip_text = tip_text[:48].rstrip("、。 ") + "。"
        text = render(title, tip_text, review)
    if len(text) > maximum_length and review:
        review = f"★{product.review_average:g}" if product.review_average is not None else ""
        text = render(title, tip_text, review)
    if len(text) > maximum_length:
        excess = len(text) - maximum_length
        keep = max(8, len(title) - excess - 1)
        title = title[:keep].rstrip() + "…"
        text = render(title, tip_text, review)
    if len(text) > maximum_length:
        raise ValueError("Generated Threads post exceeds 500 characters")
    return text
