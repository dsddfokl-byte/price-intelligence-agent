"""Media hosting, URL safety, integrity, and dry-run payload tests."""

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from app.comics.media_hosting import (
    MEDIA_HASH_MISMATCH,
    MEDIA_HASH_OK,
    MEDIA_HOSTING_NOT_CONFIGURED,
    DisabledComicMediaHostingProvider,
    HostedComicAsset,
    GitHubPagesComicMediaHostingProvider,
    MediaHostingError,
    PUBLIC_COMIC_MANIFEST_INVALID,
    PUBLIC_URL_OK,
    configured_provider,
    detect_existing_media_hosting,
    load_public_comic_manifest,
    validate_public_media_url,
    validate_public_media_url_lightweight,
)
from app.comics.media_publisher import (
    ComicThreadsPublisher,
    build_comic_alt_text,
    build_image_container_payload,
)
from app.config import THREADS_IMAGE_CONTAINER_TEST_ENABLED
from scripts.preview_threads_image_post import CONTAINER_TEST_LIMIT


def public_resolver(*_args, **_kwargs):
    return [(None, None, None, None, ("93.184.216.34", 443))]


class ComicMediaHostingTests(unittest.TestCase):
    def test_disabled_provider_and_detection(self) -> None:
        with self.assertRaisesRegex(MediaHostingError, MEDIA_HOSTING_NOT_CONFIGURED):
            DisabledComicMediaHostingProvider().publish_asset("x", "comic_001", "hash")
        detection = detect_existing_media_hosting()
        self.assertTrue(detection.found)
        self.assertEqual(detection.provider, "github_pages")

    def test_github_pages_url_resolution_is_deterministic_and_safe(self) -> None:
        provider = GitHubPagesComicMediaHostingProvider()
        expected = (
            "https://dsddfokl-byte.github.io/price-intelligence-agent/"
            "assets/comics/v1/comic_001.png"
        )
        self.assertEqual(provider.expected_public_url("comic_001"), expected)
        self.assertEqual(provider.expected_public_url("comic_001"), expected)
        self.assertTrue(
            provider.expected_public_url("comic_050").endswith("/comic_050.png")
        )
        self.assertIsInstance(configured_provider(), GitHubPagesComicMediaHostingProvider)
        with self.assertRaises(MediaHostingError):
            provider.expected_public_url("../comic_001")

    def test_public_manifest_is_primary_and_invalid_manifest_is_rejected(self) -> None:
        assets = load_public_comic_manifest()
        self.assertEqual(len(assets), 50)
        self.assertRegex(assets["comic_001"].sha256, r"^[0-9a-f]{64}$")
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "manifest.json"
            invalid.write_text(json.dumps({"base_url": "https://example.com", "items": []}))
            with self.assertRaisesRegex(MediaHostingError, PUBLIC_COMIC_MANIFEST_INVALID):
                load_public_comic_manifest(invalid)

    def test_rejects_local_file_http_and_private_hosts(self) -> None:
        for url in (
            "file:///tmp/comic.png",
            "http://example.com/comic.png",
            "https://localhost/comic.png",
            "https://127.0.0.1/comic.png",
            "https://192.168.1.10/comic.png",
        ):
            with self.assertRaises(MediaHostingError):
                validate_public_media_url(url, resolver=public_resolver)

    def response(self, body: bytes, content_type: str = "image/png") -> Mock:
        response = Mock()
        response.status_code = 200
        response.url = "https://media.example.com/comic.png"
        response.headers = {"Content-Type": content_type}
        response.content = body
        return response

    def test_public_https_content_type_and_sha256_validation(self) -> None:
        body = b"png-binary"
        session = Mock()
        session.get.return_value = self.response(body)
        result = validate_public_media_url(
            "https://media.example.com/comic.png",
            expected_sha256=hashlib.sha256(body).hexdigest(),
            session=session,
            resolver=public_resolver,
        )
        self.assertEqual(result.content_type, "image/png")
        self.assertEqual(result.integrity_status, MEDIA_HASH_OK)
        session.get.return_value = self.response(body, "text/html")
        with self.assertRaises(MediaHostingError):
            validate_public_media_url(
                "https://media.example.com/comic.png",
                session=session,
                resolver=public_resolver,
            )
        session.get.return_value = self.response(body)
        with self.assertRaisesRegex(MediaHostingError, MEDIA_HASH_MISMATCH):
            validate_public_media_url(
                "https://media.example.com/comic.png",
                expected_sha256="0" * 64,
                session=session,
                resolver=public_resolver,
            )

    def test_lightweight_validation_does_not_download_body(self) -> None:
        response = self.response(b"body-not-read")
        response.url = "https://media.example.com/comic.png"
        response.headers["Content-Length"] = "123"
        session = Mock()
        session.get.return_value = response
        result = validate_public_media_url_lightweight(
            "https://media.example.com/comic.png",
            session=session,
            resolver=public_resolver,
        )
        self.assertEqual(result.integrity_status, PUBLIC_URL_OK)
        self.assertEqual(result.content_length, 123)
        self.assertEqual(result.sha256, "")
        session.get.assert_called_once_with(
            "https://media.example.com/comic.png",
            timeout=30,
            allow_redirects=True,
            stream=True,
        )
        response.close.assert_called_once()

    def test_redirect_loop_and_private_redirect_are_rejected(self) -> None:
        redirect = Mock()
        redirect.status_code = 302
        redirect.headers = {"Location": "https://127.0.0.1/comic.png"}
        session = Mock()
        session.get.return_value = redirect
        with self.assertRaises(MediaHostingError):
            validate_public_media_url(
                "https://media.example.com/comic.png",
                session=session,
                resolver=public_resolver,
            )
        self.assertEqual(session.get.call_count, 1)

        redirect.headers = {"Location": "/comic.png"}
        session.reset_mock()
        session.get.return_value = redirect
        with self.assertRaisesRegex(MediaHostingError, "redirect loop"):
            validate_public_media_url(
                "https://media.example.com/comic.png",
                session=session,
                resolver=public_resolver,
            )
        self.assertEqual(session.get.call_count, 6)

    def test_expired_url_payload_and_safe_alt_text(self) -> None:
        now = datetime.now(timezone.utc)
        hosted = HostedComicAsset(
            "comic_001", "test", "https://media.example.com/comic.png",
            "hash", now, now - timedelta(seconds=1), MEDIA_HASH_OK,
        )
        self.assertTrue(hosted.is_expired(now))
        alt = build_comic_alt_text("PLAY", "猫 おもちゃ")
        self.assertNotIn("円", alt)
        self.assertNotIn("http", alt)
        text = "商品本文\nhttps://example.invalid/affiliate\n※本投稿にはアフィリエイトリンクが含まれます。"
        payload = build_image_container_payload(
            text=text, topic_tag="猫",
            public_image_url="https://media.example.com/comic.png",
            alt_text=alt,
        )
        self.assertEqual(payload["media_type"], "IMAGE")
        self.assertEqual(payload["alt_text"], alt)
        self.assertIn("affiliate", payload["text"])
        self.assertIn("アフィリエイトリンク", payload["text"])
        self.assertNotIn("creation_id", payload)

    def test_container_status_is_read_without_publish(self) -> None:
        publisher = ComicThreadsPublisher("test-token")
        publisher.get_user_id = Mock(return_value="user-id")
        publisher._request = Mock(
            side_effect=[{"id": "creation-id"}, {"id": "creation-id", "status": "FINISHED"}]
        )
        creation_id = publisher.create_image_container(
            "safe text",
            "https://media.example.com/comic.png",
            "猫と犬の日常を描いた縦4コマ漫画",
            "猫",
        )
        self.assertEqual(publisher.get_container_status(creation_id), "FINISHED")
        self.assertEqual(publisher._request.call_count, 2)
        self.assertFalse(
            any("threads_publish" in str(call) for call in publisher._request.call_args_list)
        )
        publisher.close()
        self.assertFalse(THREADS_IMAGE_CONTAINER_TEST_ENABLED)
        self.assertEqual(CONTAINER_TEST_LIMIT, 1)


if __name__ == "__main__":
    unittest.main()
