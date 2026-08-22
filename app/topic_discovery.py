"""Official Threads keyword-search discovery with a low-frequency SQLite cache."""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from app.config import (
    THREADS_API_BASE_URL,
    TOPIC_RELEVANCE_MIN,
    TOPIC_SEARCH_CACHE_TTL_HOURS,
    TOPIC_SEARCH_LIMIT,
    TOPIC_SEARCH_MAX_REQUESTS_PER_DAY,
)
from app.publishers.threads import ThreadsAPIError, ThreadsPublisher


SEED_TOPICS = (
    "猫", "犬", "ペット", "猫のいる暮らし", "犬のいる暮らし", "猫おもちゃ",
    "犬おもちゃ", "猫砂", "ペットカメラ", "給水", "留守番", "散歩",
)
SEARCH_MODES = ("KEYWORD", "TAG")
SEARCH_TYPES = ("TOP", "RECENT")


@dataclass(frozen=True)
class TopicSearchResult:
    query: str
    search_mode: str
    search_type: str
    posts: Sequence[Dict[str, Any]]
    source: str


class TopicDiscoveryClient(ThreadsPublisher):
    def search(self, query: str, search_mode: str, search_type: str) -> List[Dict[str, Any]]:
        if search_mode not in SEARCH_MODES or search_type not in SEARCH_TYPES:
            raise ValueError("Unsupported Threads topic search mode or type")
        payload = self._request(
            "GET", "/keyword_search", stage="keyword search",
            params={"q": query, "search_mode": search_mode, "search_type": search_type,
                    "fields": "id,text,timestamp", "limit": TOPIC_SEARCH_LIMIT},
        )
        data = payload.get("data")
        if not isinstance(data, list):
            raise ThreadsAPIError("Threads keyword search returned an unexpected structure", stage="keyword search")
        return [item for item in data if isinstance(item, dict)]


class TopicDiscovery:
    def __init__(self, connection: Any, client: Optional[TopicDiscoveryClient] = None) -> None:
        self.connection = connection
        self.client = client

    @staticmethod
    def _key(query: str, mode: str, search_type: str) -> str:
        return hashlib.sha256(f"{query}|{mode}|{search_type}".encode()).hexdigest()

    def _cached(self, query: str, mode: str, search_type: str, now: datetime) -> Optional[TopicSearchResult]:
        row = self.connection.execute(
            "SELECT * FROM topic_search_cache WHERE cache_key=?", (self._key(query, mode, search_type),)
        ).fetchone()
        if row is None or datetime.fromisoformat(row["fetched_at"]) < now - timedelta(hours=TOPIC_SEARCH_CACHE_TTL_HOURS):
            return None
        return TopicSearchResult(query, mode, search_type, json.loads(row["payload_json"]), "CACHE")

    def search(self, query: str, mode: str, search_type: str, now: Optional[datetime] = None) -> TopicSearchResult:
        current = now or datetime.now(timezone.utc)
        cached = self._cached(query, mode, search_type, current)
        if cached:
            return cached
        since = (current - timedelta(days=1)).isoformat()
        requests_today = self.connection.execute(
            "SELECT COUNT(*) FROM topic_search_cache WHERE fetched_at>=?", (since,)
        ).fetchone()[0]
        if self.client is None or requests_today >= TOPIC_SEARCH_MAX_REQUESTS_PER_DAY:
            return TopicSearchResult(query, mode, search_type, (), "CAPABILITY_UNAVAILABLE")
        try:
            posts = self.client.search(query, mode, search_type)
        except ThreadsAPIError:
            with self.connection:
                self.connection.execute(
                    "INSERT OR REPLACE INTO topic_search_cache VALUES (?, ?, ?, ?, 0, '[]', ?)",
                    (self._key(query, mode, search_type), query, mode, search_type, current.isoformat()),
                )
            raise
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO topic_search_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
                (self._key(query, mode, search_type), query, mode, search_type, len(posts),
                 json.dumps(posts, ensure_ascii=False), current.isoformat()),
            )
        return TopicSearchResult(query, mode, search_type, posts, "API")


def topic_candidates(category: str) -> List[str]:
    values = [category.replace(" ", ""), category, *SEED_TOPICS]
    return list(dict.fromkeys(value for value in values if value))


def relevance_score(topic: str, category: str, recent: bool, top: bool, historical_views: Optional[float]) -> float:
    compact = category.replace(" ", "")
    score = 0.0
    if topic == category or topic == compact:
        score += 60
    elif any(token and token in topic for token in category.split()):
        score += 35
    if recent:
        score += 15
    if top:
        score += 15
    if historical_views is not None and historical_views >= 50:
        score += 10
    return score


def choose_relevant_topic(category: str, availability: Dict[str, Dict[str, bool]], historical: Dict[str, float]) -> Optional[str]:
    scored = [
        (relevance_score(topic, category, flags.get("RECENT", False), flags.get("TOP", False), historical.get(topic)), topic)
        for topic, flags in availability.items()
    ]
    if not scored:
        return None
    score, topic = max(scored, key=lambda item: (item[0], item[1]))
    return topic if score >= TOPIC_RELEVANCE_MIN else None


def discover_growth_topic(database: Any, token: str, category: str, fallback: str, now: datetime) -> Optional[str]:
    """Resolve one relevant tag; API or permission failure safely means no tag."""
    availability = {fallback: {"TOP": False, "RECENT": False}}
    try:
        with TopicDiscoveryClient(token) as client:
            discovery = TopicDiscovery(database.connection, client)
            for search_type in SEARCH_TYPES:
                result = discovery.search(fallback, "TAG", search_type, now)
                availability[fallback][search_type] = bool(result.posts)
        row = database.connection.execute(
            "SELECT AVG(views) FROM threads_posts WHERE status='published' AND topic_tag=? AND views IS NOT NULL",
            (fallback,),
        ).fetchone()
        historical = {fallback: float(row[0])} if row and row[0] is not None else {}
        return choose_relevant_topic(category, availability, historical)
    except (ThreadsAPIError, ValueError):
        return None
