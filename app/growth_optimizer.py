"""Read-only Threads growth diagnostics and shadow experiment evaluation."""

import math
import random
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from statistics import mean, median
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.config import (
    DISTRIBUTION_EXIT_MEDIAN_VIEWS,
    GROWTH_BOOTSTRAP_SEED,
    GROWTH_MIN_PRACTICAL_UPLIFT,
    GROWTH_MIN_RUNTIME_DAYS,
    GROWTH_MIN_SAMPLES_PER_ARM,
    GROWTH_OPTIMIZER_MODE,
    THREADS_PUBLISHING,
)


WINDOW_DAYS = (1, 7, 14, 28)
METRIC_NAMES = (
    "views", "likes", "replies", "reposts", "quotes", "clicks",
    "confirmed_orders", "confirmed_commission",
)

MUTATION_ALLOWLIST = frozenset({
    "post_intent_allocation", "topic_tag_candidate", "hook_template",
    "question_presence", "text_length_target", "comic_selection_weight",
    "posting_slot_preference", "cta_template",
})
MUTATION_DENYLIST = frozenset({
    "daily_max_posts_above_4", "cycle_max_posts_above_1",
    "deal_score_threshold", "affiliate_disclosure", "duplicate_constraints",
    "cron", "api_endpoint", "authentication", "db_schema", "source_code",
    "github_hosting",
})


class Bottleneck(str, Enum):
    DISTRIBUTION = "DISTRIBUTION"
    ENGAGEMENT = "ENGAGEMENT"
    CLICK = "CLICK"
    CONVERSION = "CONVERSION"
    MONETIZATION = "MONETIZATION"
    STABLE = "STABLE"


class ExperimentStatus(str, Enum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
    READY_FOR_EVALUATION = "READY_FOR_EVALUATION"
    ADOPT = "ADOPT"
    REJECT = "REJECT"
    INCONCLUSIVE = "INCONCLUSIVE"
    PAUSED = "PAUSED"


@dataclass(frozen=True)
class DistributionStats:
    count: int
    mean: Optional[float]
    median: Optional[float]
    p25: Optional[float]
    p75: Optional[float]
    p90: Optional[float]
    trimmed_mean: Optional[float]


@dataclass(frozen=True)
class DimensionStats:
    posts: int
    observed_views: int
    median_views: Optional[float]


@dataclass(frozen=True)
class WindowMetrics:
    days: int
    posts: int
    distribution: DistributionStats
    totals: Mapping[str, Optional[float]]
    per_post: Mapping[str, Optional[float]]
    dimensions: Mapping[str, Mapping[str, DimensionStats]] = field(default_factory=dict)


@dataclass(frozen=True)
class GrowthDiagnosis:
    mode: str
    current_bottleneck: Bottleneck
    windows: Mapping[int, WindowMetrics]
    next_experiment_proposal: str
    reason: str


@dataclass(frozen=True)
class EvaluationResult:
    status: ExperimentStatus
    decision: str
    reason: str
    control_n: int
    treatment_n: int
    control_median: Optional[float]
    treatment_median: Optional[float]
    control_trimmed_mean: Optional[float]
    treatment_trimmed_mean: Optional[float]
    relative_uplift: Optional[float]
    bootstrap_ci: Optional[Tuple[float, float]]


def percentile(values: Sequence[float], probability: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def trimmed_mean(values: Sequence[float], proportion: float = 0.10) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    trim = int(len(ordered) * proportion)
    retained = ordered[trim:len(ordered) - trim] if trim else ordered
    return mean(retained)


def distribution_stats(values: Sequence[float]) -> DistributionStats:
    observed = [float(value) for value in values]
    return DistributionStats(
        count=len(observed),
        mean=mean(observed) if observed else None,
        median=median(observed) if observed else None,
        p25=percentile(observed, 0.25),
        p75=percentile(observed, 0.75),
        p90=percentile(observed, 0.90),
        trimmed_mean=trimmed_mean(observed),
    )


def _nullable_total(rows: Sequence[sqlite3.Row], metric: str) -> Optional[float]:
    values = [float(row[metric]) for row in rows if row[metric] is not None]
    return sum(values) if values else None


def _nullable_average(rows: Sequence[sqlite3.Row], metric: str) -> Optional[float]:
    values = [float(row[metric]) for row in rows if row[metric] is not None]
    return mean(values) if values else None


def _posting_slot(posted_at: str) -> str:
    hour = datetime.fromisoformat(posted_at).astimezone(
        ZoneInfo(THREADS_PUBLISHING.daily_timezone)
    ).hour
    return f"{hour:02d}:00"


def _dimension_summaries(rows: Sequence[sqlite3.Row]) -> Dict[str, Dict[str, DimensionStats]]:
    extractors = {
        "media_variant": lambda row: row["delivered_media_variant"],
        "comic_id": lambda row: row["comic_id"],
        "category": lambda row: row["search_keyword"],
        "topic_tag": lambda row: row["topic_tag"],
        "posting_slot": lambda row: _posting_slot(row["posted_at"]),
    }
    result: Dict[str, Dict[str, DimensionStats]] = {}
    for dimension, extractor in extractors.items():
        groups: Dict[str, List[sqlite3.Row]] = {}
        for row in rows:
            value = extractor(row)
            if value is not None:
                groups.setdefault(str(value), []).append(row)
        result[dimension] = {}
        for value, group in groups.items():
            views = [float(row["views"]) for row in group if row["views"] is not None]
            result[dimension][value] = DimensionStats(
                posts=len(group),
                observed_views=len(views),
                median_views=median(views) if views else None,
            )
    return result


def compute_window_metrics(
    connection: sqlite3.Connection, now: datetime, days: int
) -> WindowMetrics:
    since = (now - timedelta(days=days)).isoformat()
    rows = connection.execute(
        """
        SELECT posted_at, views, likes, replies, reposts, quotes, clicks,
               confirmed_orders, confirmed_commission, delivered_media_variant,
               comic_id, search_keyword, topic_tag
        FROM threads_posts
        WHERE status = 'published' AND posted_at >= ? AND posted_at <= ?
        ORDER BY posted_at
        """,
        (since, now.isoformat()),
    ).fetchall()
    views = [float(row["views"]) for row in rows if row["views"] is not None]
    totals = {metric: _nullable_total(rows, metric) for metric in METRIC_NAMES}
    per_post = {metric: _nullable_average(rows, metric) for metric in METRIC_NAMES}
    return WindowMetrics(
        days=days,
        posts=len(rows),
        distribution=distribution_stats(views),
        totals=totals,
        per_post=per_post,
        dimensions=_dimension_summaries(rows),
    )


def determine_bottleneck(metrics: WindowMetrics) -> Tuple[Bottleneck, str]:
    median_views = metrics.distribution.median
    if median_views is None or median_views < DISTRIBUTION_EXIT_MEDIAN_VIEWS:
        return Bottleneck.DISTRIBUTION, (
            f"7d median views={median_views if median_views is not None else 'NULL'} "
            f"is below {DISTRIBUTION_EXIT_MEDIAN_VIEWS:g}"
        )
    replies = metrics.per_post.get("replies")
    reposts = metrics.per_post.get("reposts")
    if replies is None or reposts is None or replies + reposts < 0.10:
        return Bottleneck.ENGAGEMENT, "Distribution cleared; deep engagement signal is insufficient"
    clicks = metrics.per_post.get("clicks")
    if clicks is None or clicks < 0.10:
        return Bottleneck.CLICK, "Engagement cleared; click signal is unavailable or low"
    orders = metrics.per_post.get("confirmed_orders")
    if orders is None or orders < 0.05:
        return Bottleneck.CONVERSION, "Clicks exist; confirmed order signal is unavailable or low"
    commission = metrics.totals.get("confirmed_commission")
    if commission is None or commission <= 0:
        return Bottleneck.MONETIZATION, "Orders exist; confirmed commission is unavailable or zero"
    return Bottleneck.STABLE, "No configured funnel exit threshold is currently failing"


def proposal_for(bottleneck: Bottleneck) -> str:
    return {
        Bottleneck.DISTRIBUTION: "Shadow proposal: compare hook templates without changing production",
        Bottleneck.ENGAGEMENT: "Shadow proposal: test question presence",
        Bottleneck.CLICK: "Shadow proposal: compare CTA templates",
        Bottleneck.CONVERSION: "Shadow proposal: diagnose product/category fit",
        Bottleneck.MONETIZATION: "Shadow proposal: validate commission attribution",
        Bottleneck.STABLE: "No experiment proposed; continue monitoring",
    }[bottleneck]


def diagnose(connection: sqlite3.Connection, now: datetime) -> GrowthDiagnosis:
    windows = {days: compute_window_metrics(connection, now, days) for days in WINDOW_DAYS}
    bottleneck, reason = determine_bottleneck(windows[7])
    return GrowthDiagnosis(
        mode=GROWTH_OPTIMIZER_MODE,
        current_bottleneck=bottleneck,
        windows=windows,
        next_experiment_proposal=proposal_for(bottleneck),
        reason=reason,
    )


def bootstrap_difference_ci(
    control: Sequence[float],
    treatment: Sequence[float],
    *,
    iterations: int = 2000,
    seed: int = GROWTH_BOOTSTRAP_SEED,
) -> Optional[Tuple[float, float]]:
    if not control or not treatment:
        return None
    rng = random.Random(seed)
    differences = []
    for _ in range(iterations):
        control_sample = [rng.choice(control) for _ in control]
        treatment_sample = [rng.choice(treatment) for _ in treatment]
        differences.append(median(treatment_sample) - median(control_sample))
    return (
        float(percentile(differences, 0.025)),
        float(percentile(differences, 0.975)),
    )


def evaluate_views_experiment(
    control: Sequence[float],
    treatment: Sequence[float],
    runtime_days: float,
) -> EvaluationResult:
    control_values = [float(value) for value in control]
    treatment_values = [float(value) for value in treatment]
    control_median = median(control_values) if control_values else None
    treatment_median = median(treatment_values) if treatment_values else None
    control_trimmed = trimmed_mean(control_values)
    treatment_trimmed = trimmed_mean(treatment_values)
    if (
        len(control_values) < GROWTH_MIN_SAMPLES_PER_ARM
        or len(treatment_values) < GROWTH_MIN_SAMPLES_PER_ARM
        or runtime_days < GROWTH_MIN_RUNTIME_DAYS
    ):
        return EvaluationResult(
            ExperimentStatus.INSUFFICIENT_SAMPLE, "NONE",
            "Minimum sample or runtime has not been reached",
            len(control_values), len(treatment_values), control_median,
            treatment_median, control_trimmed, treatment_trimmed, None, None,
        )
    assert control_median is not None and treatment_median is not None
    assert control_trimmed is not None and treatment_trimmed is not None
    denominator = max(abs(control_median), 1.0)
    uplift = (treatment_median - control_median) / denominator
    interval = bootstrap_difference_ci(control_values, treatment_values)
    robust_uplift = (
        uplift >= GROWTH_MIN_PRACTICAL_UPLIFT
        and treatment_trimmed >= control_trimmed * (1 + GROWTH_MIN_PRACTICAL_UPLIFT)
        and interval is not None and interval[0] > 0
    )
    if robust_uplift:
        status, decision = ExperimentStatus.ADOPT, "ADOPT_CANDIDATE"
        reason = "Median, trimmed mean, practical uplift, and bootstrap CI agree"
    elif uplift <= 0 and interval is not None and interval[1] <= 0:
        status, decision = ExperimentStatus.REJECT, "REJECT_CANDIDATE"
        reason = "Treatment does not improve the robust views distribution"
    else:
        status, decision = ExperimentStatus.INCONCLUSIVE, "INCONCLUSIVE"
        reason = "Evidence is not robust enough for adoption or rejection"
    return EvaluationResult(
        status, decision, reason, len(control_values), len(treatment_values),
        control_median, treatment_median, control_trimmed, treatment_trimmed,
        uplift, interval,
    )


class GrowthExperimentRegistry:
    """Registry only; it never changes production posting behavior."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create_planned(
        self,
        experiment_family: str,
        control_arm: str,
        treatment_arm: str,
        primary_metric: str = "median_views_7d",
        *,
        epoch: str = "growth-shadow-v1",
        created_at: Optional[datetime] = None,
        experiment_id: Optional[str] = None,
    ) -> str:
        if GROWTH_OPTIMIZER_MODE != "shadow":
            raise RuntimeError("Phase 1 registry requires shadow mode")
        identifier = experiment_id or str(uuid.uuid4())
        timestamp = (created_at or datetime.now().astimezone()).isoformat()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO growth_experiments(
                    experiment_id, experiment_family, epoch, status,
                    control_arm, treatment_arm, primary_metric,
                    minimum_samples_per_arm, minimum_runtime_days, created_at
                ) VALUES (?, ?, ?, 'PLANNED', ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier, experiment_family, epoch, control_arm,
                    treatment_arm, primary_metric, GROWTH_MIN_SAMPLES_PER_ARM,
                    GROWTH_MIN_RUNTIME_DAYS, timestamp,
                ),
            )
        return identifier

    def active(self) -> Optional[sqlite3.Row]:
        return self.connection.execute(
            """
            SELECT * FROM growth_experiments
            WHERE status IN ('PLANNED','RUNNING','INSUFFICIENT_SAMPLE','READY_FOR_EVALUATION')
            ORDER BY created_at DESC LIMIT 1
            """
        ).fetchone()


def arm_views(
    connection: sqlite3.Connection,
    column: str,
    since: datetime,
    now: datetime,
) -> Dict[str, List[float]]:
    if column not in {"template_variant", "delivered_media_variant"}:
        raise ValueError("Unsupported historical arm column")
    rows = connection.execute(
        f"""
        SELECT {column} arm, views FROM threads_posts
        WHERE status='published' AND posted_at >= ? AND posted_at <= ?
          AND {column} IS NOT NULL AND views IS NOT NULL
        """,
        (since.isoformat(), now.isoformat()),
    ).fetchall()
    result: Dict[str, List[float]] = {}
    for row in rows:
        result.setdefault(str(row["arm"]), []).append(float(row["views"]))
    return result
