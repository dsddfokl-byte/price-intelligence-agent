#!/usr/bin/env python3
"""Register the approved 01-50 PNG stock without modifying image files."""

import hashlib
import json
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STOCK_ROOT = PROJECT_ROOT / "assets" / "comics" / "stock" / "v1"
OUTPUT = PROJECT_ROOT / "config" / "comic_stock_manifest.json"
ALL_CATEGORIES = (
    "猫 フード", "猫砂", "ペットシーツ", "犬 フード", "犬 おやつ", "猫 おやつ",
    "犬 おもちゃ", "猫 おもちゃ", "ペット 自動給餌器", "ペット 給水器",
    "ペットカメラ", "ペット トイレ",
)


GROUPS = {
    "CLEANING": ({1, 11, 42}, {"猫砂": 100, "ペット トイレ": 100, "ペットシーツ": 40}),
    "UTILITY": ({2, 12, 36, 45}, {"ペット 給水器": 100, "猫 フード": 25, "犬 フード": 25}),
    "FOOD": ({3, 7, 13, 14, 17}, {"猫 フード": 100, "犬 フード": 100, "ペット 自動給餌器": 40}),
    "PLAY": ({5, 6, 9, 15, 18, 23, 32, 35, 39, 48}, {"犬 おもちゃ": 100, "猫 おもちゃ": 100}),
    "UTILITY_SHEETS": ({8, 19, 37}, {"ペットシーツ": 100, "ペット トイレ": 60}),
    "FOOD_TREATS": ({20, 25, 31, 34}, {"犬 おやつ": 100, "猫 おやつ": 100}),
    "MONITORING": ({16, 22, 40}, {"ペットカメラ": 100}),
    "FEEDER": ({26}, {"ペット 自動給餌器": 100, "猫 フード": 40, "犬 フード": 40}),
}


def metadata(index: int) -> tuple:
    for name, (indices, scores) in GROUPS.items():
        if index in indices:
            theme = {
                "UTILITY_SHEETS": "UTILITY", "FOOD_TREATS": "FOOD", "FEEDER": "FOOD"
            }.get(name, name)
            return theme, dict(scores), name.lower()
    return "HEARTWARMING", {category: 20 for category in ALL_CATEGORIES}, "pet_life"


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main() -> int:
    paths = sorted(STOCK_ROOT.glob("*.png"))
    numbered = {}
    for path in paths:
        match = re.match(r"^(\d{2})_", path.name)
        if not match:
            raise RuntimeError(f"Unnumbered PNG: {path.name}")
        index = int(match.group(1))
        if index in numbered:
            raise RuntimeError(f"Duplicate stock number: {index:02d}")
        numbered[index] = path
    missing = sorted(set(range(1, 51)) - set(numbered))
    extra = sorted(set(numbered) - set(range(1, 51)))
    if missing or extra or len(numbered) != 50:
        raise RuntimeError(f"Stock sequence invalid: missing={missing}, extra={extra}")
    items = []
    for index in range(1, 51):
        path = numbered[index]
        theme, scores, scenario = metadata(index)
        topics = sorted({"猫" if key.startswith("猫") else "犬" if key.startswith("犬") else "ペット" for key in scores})
        items.append({
            "comic_id": f"comic_{index:03d}", "file": path.name,
            "sha256": digest(path), "enabled": True,
            "category_tags": list(scores), "category_scores": scores,
            "topic_tags": topics, "theme": theme, "mood": "funny" if theme in {"PLAY", "PET_ABSURD"} else "warm",
            "characters": ["CAT", "DOG", "OWNER"], "scenario_tags": [scenario],
            "reuse_cooldown_days": 30,
        })
    payload = {"stock_version": "comic_stock_v1", "asset_root": "assets/comics/stock/v1", "items": items}
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"registered={len(items)} output={OUTPUT.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
