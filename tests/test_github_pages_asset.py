"""Regression checks for the first GitHub Pages comic asset."""

import hashlib
import json
import unittest
from pathlib import Path

from app.comics.media_hosting import GitHubPagesComicMediaHostingProvider
from app.config import (
    COMIC_MEDIA_HOSTING_PROVIDER,
    COMIC_STOCK_MANIFEST_PATH,
    COMIC_STOCK_PUBLISHING_ENABLED,
    PROJECT_ROOT,
)


class GitHubPagesAssetTests(unittest.TestCase):
    def test_all_public_copies_match_manifest_sources(self) -> None:
        stock = json.loads(COMIC_STOCK_MANIFEST_PATH.read_text(encoding="utf-8"))
        public_path = PROJECT_ROOT / "config" / "comic_public_manifest.json"
        public = json.loads(public_path.read_text(encoding="utf-8"))
        expected_ids = [f"comic_{index:03d}" for index in range(1, 51)]
        self.assertEqual([row["comic_id"] for row in stock["items"]], expected_ids)
        self.assertEqual([row["comic_id"] for row in public["items"]], expected_ids)
        self.assertEqual(len({row["url"] for row in public["items"]}), 50)

        stock_by_id = {row["comic_id"]: row for row in stock["items"]}
        for row in public["items"]:
            comic_id = row["comic_id"]
            self.assertEqual(row["file"], f"{comic_id}.png")
            self.assertEqual(
                row["url"],
                f"{public['base_url']}/{comic_id}.png",
            )
            self.assertTrue(row["url"].startswith("https://"))
            self.assertEqual(Path(row["file"]).name, row["file"])
            source_item = stock_by_id[comic_id]
            source = PROJECT_ROOT / stock["asset_root"] / source_item["file"]
            public_copy = PROJECT_ROOT / "assets" / "comics" / "v1" / row["file"]
            self.assertTrue(source.is_file())
            self.assertTrue(public_copy.is_file())
            self.assertGreater(public_copy.stat().st_size, 0)
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            public_hash = hashlib.sha256(public_copy.read_bytes()).hexdigest()
            self.assertEqual(source_hash, source_item["sha256"])
            self.assertEqual(row["sha256"], source_hash)
            self.assertEqual(public_hash, source_hash)

    def test_expected_url_and_disabled_flags(self) -> None:
        provider = GitHubPagesComicMediaHostingProvider()
        urls = [provider.expected_public_url(f"comic_{index:03d}") for index in range(1, 51)]
        self.assertEqual(len(set(urls)), 50)
        self.assertEqual(
            urls[0],
            "https://dsddfokl-byte.github.io/price-intelligence-agent/"
            "assets/comics/v1/comic_001.png",
        )
        self.assertTrue(all(url.startswith("https://") for url in urls))
        self.assertEqual(COMIC_MEDIA_HOSTING_PROVIDER, "disabled")
        self.assertFalse(COMIC_STOCK_PUBLISHING_ENABLED)


if __name__ == "__main__":
    unittest.main()
