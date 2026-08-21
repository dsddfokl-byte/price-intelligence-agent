"""Affiliate disclosure placement and single IMAGE payload tests."""

import unittest
from unittest.mock import Mock

from app.autopilot import StateValidationError, validate_publish_payload
from app.comics.media_publisher import build_image_container_payload
from app.post_text import (
    AFFILIATE_DISCLOSURE,
    has_valid_affiliate_disclosure_layout,
    normalize_affiliate_disclosure,
)
from app.models import Product
from app.publishers.threads import ThreadsPublisher
from app.thread_content import OWNER_VALUE, PRICE_CONTROL, OwnerTip, generate_experiment_text


URL = "https://example.invalid/affiliate"


class PostTextLayoutTests(unittest.TestCase):
    def test_both_production_templates_end_with_one_disclosure(self) -> None:
        product = Product(
            item_code="shop:item", item_name="猫砂 6L", item_price=1980,
            shop_code="shop", shop_name="shop", item_url="https://example.invalid/item",
            affiliate_url=URL, review_average=4.7, review_count=100,
            affiliate_rate=4.0, availability=1, fetched_at="2026-08-21T00:00:00+00:00",
        )
        tip = OwnerTip("tip", "猫砂", "内容量と材質表示を確認します。", None, None)
        for variant, selected_tip in ((PRICE_CONTROL, None), (OWNER_VALUE, tip)):
            text = generate_experiment_text(
                product, 80.0, "猫砂", variant, selected_tip, None
            )
            self.assertEqual(text.strip().splitlines()[-1], AFFILIATE_DISCLOSURE)
            self.assertEqual(text.count(AFFILIATE_DISCLOSURE), 1)
            self.assertLess(text.index(URL), text.index(AFFILIATE_DISCLOSURE))

    def test_normalization_moves_one_disclosure_to_final_line(self) -> None:
        original = (
            f"商品紹介\n{AFFILIATE_DISCLOSURE}\n#猫用品\n{URL}\n"
            f"CTA\n{AFFILIATE_DISCLOSURE}\n\n"
        )
        normalized = normalize_affiliate_disclosure(original)
        self.assertEqual(normalized.count(AFFILIATE_DISCLOSURE), 1)
        self.assertEqual(normalized.strip().splitlines()[-1], AFFILIATE_DISCLOSURE)
        self.assertLess(normalized.index("#猫用品"), normalized.index(AFFILIATE_DISCLOSURE))
        self.assertLess(normalized.index("CTA"), normalized.index(AFFILIATE_DISCLOSURE))
        self.assertLess(normalized.index(URL), normalized.index(AFFILIATE_DISCLOSURE))
        self.assertTrue(has_valid_affiliate_disclosure_layout(normalized))
        validate_publish_payload(normalized, URL)

    def test_validation_rejects_content_after_disclosure(self) -> None:
        for suffix in ("CTA", "#猫用品", "https://example.invalid/after"):
            malformed = f"商品\n{URL}\n{AFFILIATE_DISCLOSURE}\n{suffix}"
            with self.assertRaises(StateValidationError):
                validate_publish_payload(malformed, URL)

    def test_image_payload_is_one_image_container_with_complete_text(self) -> None:
        text = f"商品紹介\n{AFFILIATE_DISCLOSURE}\n補足\n{URL}"
        payload = build_image_container_payload(
            text=text,
            topic_tag="猫",
            public_image_url="https://media.example.com/comic.png",
            alt_text="猫と犬の日常を描いた縦4コマ漫画",
        )
        self.assertEqual(payload["media_type"], "IMAGE")
        self.assertEqual(payload["image_url"], "https://media.example.com/comic.png")
        self.assertEqual(payload["alt_text"], "猫と犬の日常を描いた縦4コマ漫画")
        self.assertEqual(payload["topic_tag"], "猫")
        self.assertIn(URL, payload["text"])
        self.assertTrue(payload["text"].endswith(AFFILIATE_DISCLOSURE))
        self.assertEqual(payload["text"].count(AFFILIATE_DISCLOSURE), 1)

    def test_text_container_uses_same_final_layout_without_media(self) -> None:
        publisher = ThreadsPublisher("test-token")
        publisher.get_user_id = Mock(return_value="user-id")
        publisher._request = Mock(return_value={"id": "container-id"})
        normalized = normalize_affiliate_disclosure(
            f"商品\n{AFFILIATE_DISCLOSURE}\n{URL}"
        )
        publisher.create_text_container(normalized, topic_tag="猫")
        params = publisher._request.call_args.kwargs["params"]
        self.assertEqual(params["media_type"], "TEXT")
        self.assertNotIn("image_url", params)
        self.assertEqual(params["topic_tag"], "猫")
        self.assertTrue(params["text"].endswith(AFFILIATE_DISCLOSURE))
        self.assertEqual(params["text"].count(AFFILIATE_DISCLOSURE), 1)
        self.assertLess(params["text"].index(URL), params["text"].index(AFFILIATE_DISCLOSURE))
        publisher.close()


if __name__ == "__main__":
    unittest.main()
