"""Credential-safe client for Threads post insights."""

import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import requests

from app.config import REQUEST_TIMEOUT, THREADS_API_BASE_URL


METRICS = ("views", "likes", "replies", "reposts", "quotes", "shares")


class ThreadsInsightsError(RuntimeError):
    """A safe Threads Insights API error."""


@dataclass(frozen=True)
class PostInsights:
    views: Optional[int] = None
    likes: Optional[int] = None
    replies: Optional[int] = None
    reposts: Optional[int] = None
    quotes: Optional[int] = None
    shares: Optional[int] = None


class ThreadsInsightsClient:
    def __init__(
        self,
        access_token: str,
        api_base_url: str = THREADS_API_BASE_URL,
        timeout: int = REQUEST_TIMEOUT,
    ) -> None:
        if not access_token:
            raise ValueError("Threads access token must not be empty")
        self._access_token = access_token
        self.api_base_url = api_base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {access_token}"})

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "ThreadsInsightsClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _redact(self, value: Any) -> str:
        return str(value).replace(self._access_token, "[REDACTED]").replace(
            "\r", " "
        ).replace("\n", " ")

    def _safe_error(self, response: requests.Response) -> ThreadsInsightsError:
        fields = []
        try:
            payload = response.json()
        except (requests.exceptions.JSONDecodeError, ValueError):
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            error = payload["error"]
            for name in ("message", "type", "code", "error_subcode"):
                if error.get(name) is not None:
                    fields.append(f"{name}={self._redact(error[name])}")
        detail = ", ".join(fields) if fields else "no safe error details"
        return ThreadsInsightsError(
            f"Threads Insights API returned HTTP {response.status_code}: {detail}"
        )

    def get_post_insights(self, post_id: str) -> PostInsights:
        if not post_id:
            raise ValueError("post_id must not be empty")
        response: Optional[requests.Response] = None
        for attempt in range(3):
            try:
                response = self.session.get(
                    f"{self.api_base_url}/{post_id}/insights",
                    params={"metric": ",".join(METRICS)},
                    timeout=self.timeout,
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == 2:
                    raise ThreadsInsightsError(
                        "Threads Insights API failed after 3 attempts due to "
                        "a temporary network error"
                    ) from None
                time.sleep(2**attempt)
                continue
            except requests.RequestException:
                raise ThreadsInsightsError(
                    "Threads Insights API request failed"
                ) from None

            if 200 <= response.status_code < 300:
                break
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < 2:
                    time.sleep(2**attempt)
                    continue
            raise self._safe_error(response)

        if response is None or not 200 <= response.status_code < 300:
            raise ThreadsInsightsError("Threads Insights API request failed")
        try:
            payload = response.json()
        except (requests.exceptions.JSONDecodeError, ValueError):
            raise ThreadsInsightsError(
                "Threads Insights API returned invalid JSON"
            ) from None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ThreadsInsightsError(
                "Threads Insights API returned an unexpected JSON structure"
            )

        values: Dict[str, Optional[int]] = {name: None for name in METRICS}
        for metric in data:
            if not isinstance(metric, dict) or metric.get("name") not in values:
                continue
            raw_value: Any = None
            metric_values = metric.get("values")
            if isinstance(metric_values, list) and metric_values:
                first = metric_values[0]
                if isinstance(first, dict):
                    raw_value = first.get("value")
            total_value = metric.get("total_value")
            if raw_value is None and isinstance(total_value, dict):
                raw_value = total_value.get("value")
            try:
                values[str(metric["name"])] = (
                    int(raw_value) if raw_value is not None else None
                )
            except (TypeError, ValueError):
                values[str(metric["name"])] = None
        return PostInsights(**values)

    def get_conversation_metrics(self, post_id: str) -> Tuple[int, int]:
        """Return unique repliers and maximum reply depth for an owned post."""
        try:
            response = self.session.get(
                f"{self.api_base_url}/{post_id}/conversation",
                params={"fields": "id,username,replied_to,root_post", "reverse": "false"},
                timeout=self.timeout,
            )
        except requests.RequestException:
            raise ThreadsInsightsError("Threads replies API request failed") from None
        if not 200 <= response.status_code < 300:
            raise self._safe_error(response)
        try:
            payload = response.json()
        except (requests.exceptions.JSONDecodeError, ValueError):
            raise ThreadsInsightsError("Threads replies API returned invalid JSON") from None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ThreadsInsightsError("Threads replies API returned an unexpected JSON structure")
        usernames = {str(item["username"]) for item in data if isinstance(item, dict) and item.get("username")}
        by_id = {str(item["id"]): item for item in data if isinstance(item, dict) and item.get("id")}
        maximum_depth = 0
        for item in by_id.values():
            depth, seen, current = 1, set(), item
            while isinstance(current.get("replied_to"), dict):
                parent_id = str(current["replied_to"].get("id", ""))
                if not parent_id or parent_id in seen or parent_id not in by_id:
                    break
                seen.add(parent_id)
                depth += 1
                current = by_id[parent_id]
            maximum_depth = max(maximum_depth, depth)
        return len(usernames), maximum_depth
