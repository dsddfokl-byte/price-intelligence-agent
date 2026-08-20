"""Regression checks for the first GitHub Pages comic asset."""

import hashlib
import json
import unittest

from app.comics.media_hosting import GitHubPagesComicMediaHostingProvider
from app.config import (
    COMIC_MEDIA_HOSTING_PROVIDER,
    COMIC_STOCK_MANIFEST_PATH,
    COMIC_STOCK_PUBLISHING_ENABLED,
    PROJECT_ROOT,
)


class GitHubPagesAssetTests(unittest.TestCase):
    def test_comic_001_public_copy_matches_manifest_source(self) -> None:
        manifest = json.loads(COMIC_STOCK_MANIFEST_PATH.read_text(encoding="utf-8"))
        item = next(row for row in manifest["items"] if row["comic_id"] == "comic_001")
        source = PROJECT_ROOT / manifest["asset_root"] / item["file"]
        public_copy = PROJECT_ROOT / "assets" / "comics" / "v1" / "comic_001.png"
        self.assertTrue(source.is_file())
        self.assertTrue(public_copy.is_file())
        source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        public_hash = hashlib.sha256(public_copy.read_bytes()).hexdigest()
        self.assertEqual(source_hash, item["sha256"])
        self.assertEqual(public_hash, source_hash)

    def test_expected_url_and_disabled_flags(self) -> None:
        url = GitHubPagesComicMediaHostingProvider().expected_public_url("comic_001")
        self.assertEqual(
            url,
            "https://dsddfokl-byte.github.io/price-intelligence-agent/"
            "assets/comics/v1/comic_001.png",
        )
        self.assertTrue(url.startswith("https://"))
        self.assertEqual(COMIC_MEDIA_HOSTING_PROVIDER, "disabled")
        self.assertFalse(COMIC_STOCK_PUBLISHING_ENABLED)


if __name__ == "__main__":
    unittest.main()
