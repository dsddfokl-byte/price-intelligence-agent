"""Safe, deterministic non-commercial Threads content for the Growth arm."""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from app.config import THREADS_PUBLISHING
from app.models import Product
from app.post_intent import PostIntent
from app.publishers.threads import ThreadsCandidate, post_text_hash


GROWTH_TEMPLATES = ("EMPATHY", "OBSERVATION", "QUESTION")
_CLOSINGS = (
    "みなさんの家でもありますか？",
    "思い当たる瞬間はありますか？",
    "その子らしい日課、何かありますか？",
    "つい笑ってしまう習慣はありますか？",
    "毎日の定番になっていることはありますか？",
    "意外と正確だなと思う習慣はありますか？",
    "静かに見守りたくなる瞬間はありますか？",
    "最近気づいた小さな変化はありますか？",
)

_CONTENT = {
    "猫 フード": (
        "ごはんの袋を開ける音だけ、どうしてあんなに聞き分けられるんでしょう。",
        "猫のごはん時間、こちらの予定より正確なことがありませんか？",
    ),
    "犬 フード": (
        "ごはん前の待っている顔には、毎回ちょっと負けそうになります。",
        "犬のごはん時間、時計より先に教えてもらうことはありませんか？",
    ),
    "猫砂": (
        "猫トイレを整えた直後に、すぐ確認しに来る姿までがひと区切り。",
        "猫砂を替えた直後、念入りに点検されることはありませんか？",
    ),
    "ペットシーツ": (
        "きれいに交換した直後ほど、ちゃんと見に来てくれる気がします。",
        "ペット用品を整えた直後、なぜかすぐ気づかれることはありませんか？",
    ),
}


@dataclass(frozen=True)
class GrowthPost:
    candidate: ThreadsCandidate
    text: str
    text_hash: str
    template: str
    topic_tag: Optional[str]
    post_intent: str = PostIntent.GROWTH.value

    @property
    def template_variant(self) -> str:
        return self.template

    @property
    def tip_id(self) -> None:
        return None

    @property
    def content_trigger(self) -> None:
        return None

    def __getattr__(self, name: str):
        return getattr(self.candidate, name)


def generate_growth_post(candidate: ThreadsCandidate, now: datetime, topic_tag: Optional[str]) -> GrowthPost:
    seed = f"{candidate.product.item_code}|{now.date().isoformat()}|growth-v1"
    digest = hashlib.sha256(seed.encode()).digest()
    template = GROWTH_TEMPLATES[digest[0] % len(GROWTH_TEMPLATES)]
    closing = _CLOSINGS[digest[1] % len(_CLOSINGS)]
    empathy, question = _CONTENT.get(
        candidate.search_keyword,
        (
            "ペットとの暮らしでは、小さな日課ほど印象に残ることがあります。",
            "毎日のペット時間で、思わず笑ってしまう瞬間はありますか？",
        ),
    )
    if template == "EMPATHY":
        text = f"🐾 ペットとの暮らしメモ\n\n{empathy}\n\n{closing}"
    elif template == "OBSERVATION":
        text = f"🐾 今日のペット観察\n\n{empathy}\n\n{closing}"
    else:
        text = f"🐾 ちょっと聞きたいこと\n\n{question}\n\n{closing}"
    if len(text) > THREADS_PUBLISHING.maximum_text_length:
        raise ValueError("Growth post exceeds Threads text limit")
    return GrowthPost(candidate, text, post_text_hash(text), template, topic_tag)


def generate_generic_growth_post(
    now: datetime,
    categories: Sequence[str],
    topic_tags: Mapping[str, str],
) -> GrowthPost:
    """Build a Growth post without an affiliate-product dependency."""
    if not categories:
        raise ValueError("Growth categories must not be empty")
    local = now.astimezone(ZoneInfo(THREADS_PUBLISHING.daily_timezone))
    slot = min((7, 12, 17, 21), key=lambda hour: abs(hour - local.hour))
    seed = f"growth-source-v1|{local.date().isoformat()}|{slot}"
    digest = hashlib.sha256(seed.encode()).digest()
    category = categories[digest[0] % len(categories)]
    content_key = f"growth:{local.date().isoformat()}:{slot}:{category}"
    source = ThreadsCandidate(
        product=Product(
            item_code=content_key,
            item_name=f"Growth content: {category}",
            item_price=None,
            shop_code=None,
            shop_name=None,
            item_url=None,
            affiliate_url=None,
            review_average=None,
            review_count=None,
            affiliate_rate=None,
            availability=None,
            fetched_at=now.isoformat(),
        ),
        deal_score=0.0,
        text="",
        text_hash="",
        reason="generic_growth_source",
        search_keyword=category,
        topic_tag=topic_tags.get(category, "ペット"),
        template_variant="GROWTH",
        tip_id=None,
        content_trigger=None,
    )
    return generate_growth_post(source, now, source.topic_tag)


def validate_growth_text(text: str) -> None:
    forbidden = ("http://", "https://", "円", "買い時スコア", "アフィリエイトリンク", "楽天市場で確認")
    if not text.strip() or len(text) > THREADS_PUBLISHING.maximum_text_length:
        raise ValueError("Growth text length is invalid")
    if any(value in text for value in forbidden):
        raise ValueError("Growth text contains commercial content")
