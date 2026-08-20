#!/usr/bin/env python3
"""Build deterministic GitHub Pages copies of the approved comic stock."""

import json
import shutil
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.comics.stock_validator import file_sha256, load_and_validate_manifest  # noqa: E402
from app.config import (  # noqa: E402
    COMIC_GITHUB_PAGES_ASSET_PREFIX,
    COMIC_GITHUB_PAGES_BASE_URL,
    COMIC_STOCK_MANIFEST_PATH,
)


PUBLIC_MANIFEST_PATH = PROJECT_ROOT / "config" / "comic_public_manifest.json"


def build_public_assets() -> int:
    validation = load_and_validate_manifest(COMIC_STOCK_MANIFEST_PATH)
    if validation.errors or len(validation.valid_assets) != 50:
        raise RuntimeError("Comic stock validation failed; public assets were not built")

    public_root = (PROJECT_ROOT / COMIC_GITHUB_PAGES_ASSET_PREFIX).resolve()
    public_root.relative_to(PROJECT_ROOT)
    public_root.mkdir(parents=True, exist_ok=True)
    base_url = (
        f"{COMIC_GITHUB_PAGES_BASE_URL.rstrip('/')}"
        f"/{COMIC_GITHUB_PAGES_ASSET_PREFIX.strip('/')}"
    )
    items = []
    for asset in validation.valid_assets:
        public_name = f"{asset.comic_id}.png"
        public_path = (public_root / public_name).resolve()
        if public_path.parent != public_root:
            raise RuntimeError("Unsafe public comic path")
        if public_path.exists():
            if file_sha256(public_path) != asset.sha256:
                raise RuntimeError(f"Existing public asset hash mismatch: {asset.comic_id}")
        else:
            shutil.copyfile(asset.file_path, public_path)
        if public_path.stat().st_size <= 0 or file_sha256(public_path) != asset.sha256:
            raise RuntimeError(f"Public asset validation failed: {asset.comic_id}")
        items.append({
            "comic_id": asset.comic_id,
            "file": public_name,
            "url": f"{base_url}/{public_name}",
            "sha256": asset.sha256,
        })

    payload = {
        "version": "comic_public_v1",
        "base_url": base_url,
        "items": items,
    }
    PUBLIC_MANIFEST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"PUBLIC_ASSET_COUNT={len(items)}")
    print(f"PUBLIC_MANIFEST={PUBLIC_MANIFEST_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(build_public_assets())
