"""Media hosting, URL safety, integrity, and dry-run payload tests."""

import hashlib
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

from app.comics.media_hosting import (
    MEDIA_HASH_MISMATCH,
    MEDIA_HASH_OK,
    MEDIA_HOSTING_NOT_CONFIGURED,
    DisabledComicMediaHostingProvider,
    HostedComicAsset,
    MediaHostingError,
    detect_existing_media_hosting,
    validate_public_media_url,
)
from app.comics.media_publisher import (
    build_comic_alt_text,
    build_image_container_payload,
)


def public_resolver(*_args, **_kwargs):
    return [(None, None, None, None, ("93.184.216.34", 443))]


class ComicMediaHostingTests(unittest.TestCase):
    def test_disabled_provider_and_detection(self) -> None:
        with self.assertRaisesRegex(MediaHostingError, MEDIA_HOSTING_NOT_CONFIGURED):
            DisabledComicMediaHostingProvider().publish_asset("x", "comic_001", "hash")
        detection = detect_existing_media_hosting()
        self.assertTrue(detection.found)
        self.assertEqual(detection.provider, "github_pages")

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


if __name__ == "__main__":
    unittest.main()
