"""Shared final-layout rules for every Threads post body."""


AFFILIATE_DISCLOSURE = "※本投稿にはアフィリエイトリンクが含まれます。"


def normalize_affiliate_disclosure(text: str) -> str:
    """Move the disclosure to the final non-empty line exactly once."""
    body = text.replace(AFFILIATE_DISCLOSURE, "").strip()
    if not body:
        return AFFILIATE_DISCLOSURE
    return f"{body}\n\n{AFFILIATE_DISCLOSURE}"


def has_valid_affiliate_disclosure_layout(text: str) -> bool:
    stripped = text.strip()
    return (
        stripped.count(AFFILIATE_DISCLOSURE) == 1
        and stripped.endswith(AFFILIATE_DISCLOSURE)
    )
