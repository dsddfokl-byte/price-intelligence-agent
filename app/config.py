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
SEARCH_TERMS_PATH = PROJECT_ROOT / "config" / "search_terms.json"
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
