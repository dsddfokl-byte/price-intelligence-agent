"""Validate the immutable, approved PNG comic stock and its manifest."""

import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import PROJECT_ROOT, THREADS_TOPIC_TAGS


ALLOWED_THEMES = {
    "PET_LIFE", "PET_ABSURD", "HEARTWARMING", "UTILITY",
    "PLAY", "FOOD", "CLEANING", "MONITORING",
}


@dataclass(frozen=True)
class ComicAsset:
    comic_id: str
    file: str
    file_path: Path
    sha256: str
    enabled: bool
    category_tags: Tuple[str, ...]
    category_scores: Dict[str, int]
    topic_tags: Tuple[str, ...]
    theme: str
    mood: str
    characters: Tuple[str, ...]
    scenario_tags: Tuple[str, ...]
    reuse_cooldown_days: int
    validation_error: Optional[str] = None


@dataclass(frozen=True)
class StockValidation:
    stock_version: str
    asset_root: Path
    assets: Tuple[ComicAsset, ...]
    errors: Tuple[str, ...]

    @property
    def valid_assets(self) -> Tuple[ComicAsset, ...]:
        return tuple(asset for asset in self.assets if not asset.validation_error)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def png_dimensions(path: Path) -> Tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError("not a readable PNG")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise ValueError("invalid PNG dimensions")
    return width, height


def _safe_asset_path(asset_root: Path, filename: str) -> Path:
    if Path(filename).name != filename or Path(filename).suffix.lower() != ".png":
        raise ValueError("unsafe or non-PNG asset path")
    path = (asset_root / filename).resolve()
    if path.parent != asset_root.resolve():
        raise ValueError("asset path traversal")
    return path


def load_and_validate_manifest(manifest_path: Path) -> StockValidation:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    root_value = payload.get("asset_root")
    if not isinstance(root_value, str):
        raise ValueError("manifest asset_root is required")
    asset_root = (PROJECT_ROOT / root_value).resolve()
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("manifest items must be a list")
    errors: List[str] = []
    assets: List[ComicAsset] = []
    ids = set()
    files = set()
    valid_categories = set(THREADS_TOPIC_TAGS)
    expected_ids = {f"comic_{index:03d}" for index in range(1, 51)}
    for raw in items:
        comic_id = str(raw.get("comic_id", ""))
        filename = str(raw.get("file", ""))
        item_errors: List[str] = []
        if comic_id in ids:
            item_errors.append("duplicate comic_id")
        if filename in files:
            item_errors.append("duplicate file")
        ids.add(comic_id)
        files.add(filename)
        try:
            path = _safe_asset_path(asset_root, filename)
        except ValueError as error:
            path = asset_root / "INVALID"
            item_errors.append(str(error))
        expected_hash = str(raw.get("sha256", ""))
        if not path.is_file():
            item_errors.append("COMIC_ASSET_MISSING")
        else:
            try:
                png_dimensions(path)
                if file_sha256(path) != expected_hash:
                    item_errors.append("COMIC_ASSET_HASH_MISMATCH")
            except (OSError, ValueError) as error:
                item_errors.append(str(error))
        categories = tuple(raw.get("category_tags", ()))
        scores = raw.get("category_scores", {})
        if not isinstance(scores, dict) or not set(scores).issubset(valid_categories):
            item_errors.append("invalid category mapping")
            scores = {}
        if not set(categories).issubset(valid_categories):
            item_errors.append("invalid category_tags")
        enabled = raw.get("enabled")
        if not isinstance(enabled, bool):
            item_errors.append("enabled must be boolean")
            enabled = False
        cooldown = raw.get("reuse_cooldown_days")
        if not isinstance(cooldown, int) or cooldown <= 0:
            item_errors.append("reuse_cooldown_days must be positive")
            cooldown = 30
        theme = str(raw.get("theme", ""))
        if theme not in ALLOWED_THEMES:
            item_errors.append("invalid theme")
        validation_error = "; ".join(item_errors) or None
        if validation_error:
            errors.append(f"{comic_id or filename}: {validation_error}")
        assets.append(ComicAsset(
            comic_id=comic_id, file=filename, file_path=path,
            sha256=expected_hash, enabled=enabled,
            category_tags=categories,
            category_scores={str(k): int(v) for k, v in scores.items()},
            topic_tags=tuple(raw.get("topic_tags", ())), theme=theme,
            mood=str(raw.get("mood", "")),
            characters=tuple(raw.get("characters", ())),
            scenario_tags=tuple(raw.get("scenario_tags", ())),
            reuse_cooldown_days=cooldown, validation_error=validation_error,
        ))
    missing_ids = sorted(expected_ids - ids)
    extra_ids = sorted(ids - expected_ids)
    if missing_ids:
        errors.append("missing comic ids: " + ", ".join(missing_ids))
    if extra_ids:
        errors.append("unexpected comic ids: " + ", ".join(extra_ids))
    disk_pngs = {path.name for path in asset_root.glob("*.png")}
    missing_files = sorted(files - disk_pngs)
    unregistered_files = sorted(disk_pngs - files)
    if missing_files:
        errors.append("missing files: " + ", ".join(missing_files))
    if unregistered_files:
        errors.append("unregistered files: " + ", ".join(unregistered_files))
    return StockValidation(
        stock_version=str(payload.get("stock_version", "")), asset_root=asset_root,
        assets=tuple(assets), errors=tuple(errors),
    )
