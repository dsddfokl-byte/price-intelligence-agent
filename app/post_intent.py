"""Deterministic Growth/Affiliate assignment for eligible posting cycles."""

from datetime import datetime
from enum import Enum
from zoneinfo import ZoneInfo

from app.config import POST_INTENT_EPOCH, THREADS_PUBLISHING
from app.config import GROWTH_MIN_RUNTIME_DAYS, GROWTH_MIN_SAMPLES_PER_ARM


class PostIntent(str, Enum):
    GROWTH = "GROWTH"
    AFFILIATE = "AFFILIATE"


POSTING_HOURS = (7, 12, 17, 21)


def posting_slot(now: datetime) -> int:
    local = now.astimezone(ZoneInfo(THREADS_PUBLISHING.daily_timezone))
    return min(range(len(POSTING_HOURS)), key=lambda index: abs(POSTING_HOURS[index] - local.hour))


def assign_post_intent(now: datetime, epoch: str = POST_INTENT_EPOCH) -> PostIntent:
    """Alternate arms by slot and rotate the starting arm deterministically by day."""
    local_date = now.astimezone(ZoneInfo(THREADS_PUBLISHING.daily_timezone)).date()
    epoch_offset = sum(epoch.encode("utf-8")) % 2
    day_bit = (local_date.toordinal() + epoch_offset) % 2
    return PostIntent.GROWTH if (posting_slot(now) + day_bit) % 2 == 0 else PostIntent.AFFILIATE


def ensure_post_intent_experiment(connection, now: datetime) -> None:
    """Idempotently register the production assignment without changing its split."""
    with connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO growth_experiments(
                experiment_id, experiment_family, epoch, status,
                control_arm, treatment_arm, started_at, primary_metric,
                minimum_samples_per_arm, minimum_runtime_days, created_at
            ) VALUES (?, 'POST_INTENT', ?, 'RUNNING', 'AFFILIATE', 'GROWTH', ?,
                      'median_views_per_post', ?, ?, ?)
            """,
            (
                f"post-intent-{POST_INTENT_EPOCH}", POST_INTENT_EPOCH,
                now.isoformat(), GROWTH_MIN_SAMPLES_PER_ARM,
                GROWTH_MIN_RUNTIME_DAYS, now.isoformat(),
            ),
        )
