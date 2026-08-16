"""Rule-based product-title formatting for social posts."""

import re
from typing import Match, Optional


TARGET_TITLE_LENGTH = 40

PROMOTION_PATTERN = re.compile(
    r"(?:"
    r"最大\s*\d+(?:\.\d+)?\s*[％%]\s*ポイントバック|"
    r"\d+(?:\.\d+)?\s*[％%]\s*(?:OFF|オフ)|"
    r"要エントリー|ポイント(?:バック|アップ|\d+倍)?|"
    r"送料無料|公認店|正規輸入品|正規品|"
    r"合わせ買い(?:で)?(?:お得)?|まとめ買い|"
    r"お得|セール|SALE|あす楽|即納"
    r")",
    re.IGNORECASE,
)

BRACKET_PATTERN = re.compile(
    r"【(?P<corner>[^】]*)】|\[(?P<square>[^]]*)\]|（(?P<wide>[^）]*)）|\((?P<round>[^)]*)\)"
)

CAPACITY_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:kg|g|ml|L|枚|個|本|袋)"
    r"(?:\s*[×xX*]\s*\d+\s*(?:個|コ|袋|本|枚)?(?:セット|入)?)?",
    re.IGNORECASE,
)


def _remove_promotional_brackets(match: Match[str]) -> str:
    content = next(value for value in match.groupdict().values() if value is not None)
    return " " if PROMOTION_PATTERN.search(content) else match.group(0)


def _normalize_title(title: str) -> str:
    title = BRACKET_PATTERN.sub(_remove_promotional_brackets, title)
    title = PROMOTION_PATTERN.sub(" ", title)
    title = re.sub(r"(?<=[A-Za-zァ-ヴー])(?=\d+(?:\.\d+)?\s*(?:kg|g|ml|L)\b)", " ", title)
    title = re.sub(r"^[\s＼／/|｜★☆◆◇■□●○・!！]+", "", title)
    title = re.sub(r"[\s　]+", " ", title)
    title = re.sub(r"\s+([,，。!！])", r"\1", title)
    return title.strip(" -–—_|｜・!！")


def _first_capacity(title: str) -> Optional[Match[str]]:
    return CAPACITY_PATTERN.search(title)


def _shorten_preserving_capacity(prefix: str, capacity: str, limit: int) -> str:
    available = limit - len(capacity) - 2
    if available < 2:
        return (prefix + " " + capacity)[:limit].rstrip()
    shortened = prefix[:available].rstrip()
    if " " in shortened and len(prefix) > available:
        word_boundary = shortened.rfind(" ")
        if word_boundary >= max(4, available // 2):
            shortened = shortened[:word_boundary].rstrip()
    return f"{shortened}… {capacity}"


def shorten_product_title(
    original_title: Optional[str],
    target_length: int = TARGET_TITLE_LENGTH,
) -> str:
    """Return a conservative, readable title for Threads posts."""
    title = _normalize_title(original_title or "商品名なし")
    if not title:
        return "商品名なし"

    capacity_match = _first_capacity(title)
    if capacity_match:
        capacity = re.sub(r"[xX*]", "×", capacity_match.group(0).replace(" ", ""))
        prefix = title[: capacity_match.start()].strip().rstrip("([（【")

        # Rakuten titles for cat litter often put the category after detailed
        # scent/material descriptions. Prefer the stable brand/category/size.
        if title.startswith("エバークリーン") and "猫砂" in title:
            title = f"エバークリーン 猫砂 {capacity}"
        else:
            title = f"{prefix} {capacity}".strip()

        if len(title) > target_length:
            title = _shorten_preserving_capacity(prefix, capacity, target_length)

    if len(title) <= target_length:
        return title

    shortened = title[:target_length].rstrip()
    if " " in shortened:
        word_boundary = shortened.rfind(" ")
        if word_boundary >= target_length // 2:
            shortened = shortened[:word_boundary].rstrip()
    return shortened.rstrip(" -–—_|｜・!！") + "…"
