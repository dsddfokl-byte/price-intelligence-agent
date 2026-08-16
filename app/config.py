"""Application configuration loaded from the project-level .env file."""

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
API_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
REQUEST_TIMEOUT = 30
DATABASE_PATH = PROJECT_ROOT / "database" / "affiliate.db"
LOG_PATH = PROJECT_ROOT / "logs" / "app.log"
AUTOMATION_LOG_PATH = PROJECT_ROOT / "logs" / "automation.log"
INSIGHTS_LOG_PATH = PROJECT_ROOT / "logs" / "threads_insights.log"
RUN_CYCLE_LOCK_PATH = PROJECT_ROOT / "data" / "run_cycle.lock"
SEARCH_TERMS_PATH = PROJECT_ROOT / "config" / "search_terms.json"
PET_OWNER_TIPS_PATH = PROJECT_ROOT / "config" / "pet_owner_tips.json"
THREADS_API_BASE_URL = "https://graph.threads.net/v1.0"
THREADS_USERNAME = "kaidoki_radar_"
THREADS_TOPIC_TAGS = {
    "猫 フード": "猫",
    "猫砂": "猫",
    "ペットシーツ": "ペット",
    "犬 フード": "犬",
}
REQUIRED_ENV_VARS = (
    "RAKUTEN_APP_ID",
    "RAKUTEN_ACCESS_KEY",
    "RAKUTEN_AFFILIATE_ID",
)


class ConfigurationError(RuntimeError):
    """Raised when required configuration is unavailable."""


@dataclass(frozen=True)
class Settings:
    application_id: str = field(repr=False)
    access_key: str = field(repr=False)
    affiliate_id: str = field(repr=False)
    api_url: str = API_URL
    timeout: int = REQUEST_TIMEOUT
    database_path: Path = DATABASE_PATH


@dataclass(frozen=True)
class ThreadsPublishingConfig:
    minimum_deal_score: float = 75.0
    minimum_review_count: int = 20
    candidate_limit: int = 5
    daily_post_limit: int = 2
    daily_timezone: str = "Asia/Tokyo"
    item_cooldown_days: int = 7
    price_drop_override: float = 0.10
    text_cooldown_days: int = 30
    maximum_text_length: int = 500


THREADS_PUBLISHING = ThreadsPublishingConfig()


def load_settings() -> Settings:
    """Load and validate settings without exposing secret values."""
    load_dotenv(dotenv_path=ENV_FILE, override=True)
    missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
    if missing:
        raise ConfigurationError(
            "Required environment variables are missing: " + ", ".join(missing)
        )
    return Settings(
        application_id=os.environ["RAKUTEN_APP_ID"],
        access_key=os.environ["RAKUTEN_ACCESS_KEY"],
        affiliate_id=os.environ["RAKUTEN_AFFILIATE_ID"],
    )


def load_threads_access_token() -> str:
    """Load the Threads token only for an explicit publishing operation."""
    load_dotenv(dotenv_path=ENV_FILE, override=True)
    token = os.getenv("THREADS_ACCESS_TOKEN")
    if not token:
        raise ConfigurationError(
            "Required environment variable is missing: THREADS_ACCESS_TOKEN"
        )
    return token
