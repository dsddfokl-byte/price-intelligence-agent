"""Official Threads IMAGE-container interface, isolated behind feature flags."""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from app.publishers.threads import ThreadsAPIError, ThreadsPostError, ThreadsPublisher


LOGGER = logging.getLogger("comic_media_publisher")


def build_image_container_payload(
    *,
    text: str,
    topic_tag: Optional[str],
    public_image_url: str,
    alt_text: str,
) -> dict:
    if not public_image_url.startswith("https://"):
        raise ThreadsPostError("Threads image requires a public HTTPS image URL")
    payload = {
        "media_type": "IMAGE",
        "image_url": public_image_url,
        "text": text,
        "alt_text": alt_text,
    }
    if topic_tag:
        payload["topic_tag"] = topic_tag
    return payload


def build_comic_alt_text(theme: str, category: str) -> str:
    category_label = category.replace(" ", "")
    theme_labels = {
        "FOOD": "食事風景",
        "PLAY": "遊び時間",
        "CLEANING": "お掃除の様子",
        "MONITORING": "見守り風景",
        "UTILITY": "ペット用品を使う様子",
        "HEARTWARMING": "日常風景",
        "PET_LIFE": "日常風景",
        "PET_ABSURD": "楽しい日常",
    }
    scene = theme_labels.get(theme, "日常風景")
    return f"猫と犬と飼い主の{scene}を描いた縦4コマ漫画（{category_label}関連）"


@dataclass(frozen=True)
class MediaPublishResult:
    post_id: str
    delivered_media_variant: str
    fallback_reason: Optional[str] = None


class ComicThreadsPublisher(ThreadsPublisher):
    """Create an IMAGE container from a public URL and safely fall back to TEXT."""

    def _create_image_container_once(
        self, text: str, image_url: str, alt_text: str, topic_tag: Optional[str]
    ) -> str:
        user_id = self.get_user_id()
        params = build_image_container_payload(
            text=text,
            topic_tag=topic_tag,
            public_image_url=image_url,
            alt_text=alt_text,
        )
        payload = self._request(
            "POST", f"/{user_id}/threads", stage="image create", params=params
        )
        creation_id = payload.get("id")
        if not creation_id:
            raise ThreadsAPIError(
                "Threads image create API response did not include a creation id",
                stage="image create",
            )
        return str(creation_id)

    def create_image_container(
        self,
        text: str,
        image_url: str,
        alt_text: str,
        topic_tag: Optional[str] = None,
    ) -> str:
        if not image_url.startswith(("https://", "http://")):
            raise ThreadsPostError("Threads image requires a public HTTP image URL")
        try:
            return self._create_image_container_once(text, image_url, alt_text, topic_tag)
        except ThreadsAPIError as error:
            topic_fallback = (
                topic_tag is not None and error.status_code is not None
                and 400 <= error.status_code < 500 and error.status_code != 429
            )
            if not topic_fallback:
                raise
            LOGGER.warning(
                "Threads image create rejected topic_tag; retrying once without topic_tag"
            )
            return self._create_image_container_once(text, image_url, alt_text, None)

    def publish_image(
        self,
        text: str,
        image_url: str,
        alt_text: str,
        topic_tag: Optional[str] = None,
    ) -> str:
        creation_id = self.create_image_container(text, image_url, alt_text, topic_tag)
        time.sleep(2)
        return self.publish_container(creation_id)

    def publish_with_optional_image(
        self,
        text: str,
        topic_tag: Optional[str] = None,
        image_url: Optional[str] = None,
        alt_text: str = "猫と犬と飼い主の日常を描いた縦4コマ漫画",
    ) -> MediaPublishResult:
        if not image_url:
            return MediaPublishResult(self.publish_text(text, topic_tag), "NO_COMIC")
        try:
            return MediaPublishResult(
                self.publish_image(text, image_url, alt_text, topic_tag), "COMIC"
            )
        except (ThreadsAPIError, ThreadsPostError) as error:
            LOGGER.warning(
                "Comic media delivery failed; falling back to text error_type=%s",
                type(error).__name__,
            )
            return MediaPublishResult(
                self.publish_text(text, topic_tag), "NO_COMIC", "IMAGE_DELIVERY_FAILED"
            )
