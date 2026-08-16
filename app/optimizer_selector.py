"""Deterministic performance re-ranking for an already-eligible candidate pool."""

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from statistics import mean, median
from typing import Dict, List, Optional, Sequence, Tuple

from app.autopilot import (
    DEEP_ENGAGEMENT_MIN_VIEWS,
    AutopilotState,
    ExecutionState,
    ExperimentArm,
    RewardMode,
)
from app.publishers.threads import ThreadsCandidate


OPTIMIZER_LOOKBACK_DAYS = 30
MIN_72H_OUTCOMES = 5
MIN_CATEGORY_SAMPLES = 3
MIN_TEMPLATE_SAMPLES = 3
PRIOR_STRENGTH = 5.0
MAX_HISTORICAL_ADJUSTMENT = 5.0
OPTIMIZER_MODEL_VERSION = "performance-selector-v1"


class OptimizerSelectorError(RuntimeError):
    """An optimizer-only failure which must fall back to production control."""


@dataclass(frozen=True)
class PerformanceOutcome:
    category: Optional[str]
    template_variant: Optional[str]
    proxy: float


@dataclass(frozen=True)
class DimensionStats:
    n: int
    mean_proxy: Optional[float]
    median_proxy: Optional[float]
    smoothed_proxy: Optional[float]
    sufficient: bool


@dataclass(frozen=True)
class OptimizerCandidateScore:
    candidate: ThreadsCandidate
    candidate_source: str
    base_quality_score: float
    global_mean_proxy: Optional[float]
    category_n: int
    category_smoothed_proxy: Optional[float]
    category_delta: Optional[float]
    template_n: int
    template_smoothed_proxy: Optional[float]
    template_delta: Optional[float]
    historical_adjustment: float
    optimizer_score: float
    rank_position: int


@dataclass(frozen=True)
class OptimizerAnalysis:
    valid_outcomes: int
    global_mean_proxy: Optional[float]
    category_stats: Dict[str, DimensionStats]
    template_stats: Dict[str, DimensionStats]
    ranking: Sequence[OptimizerCandidateScore]
    adjustment_enabled: bool


def choose_candidate_for_arm(
    candidates: Sequence[ThreadsCandidate],
    analysis: OptimizerAnalysis,
    state: AutopilotState,
    arm: ExperimentArm,
) -> Tuple[Optional[ThreadsCandidate], str]:
    if not candidates:
        return None, "NO_CANDIDATE"
    production_choice = candidates[0]
    if state.execution_state == ExecutionState.SHADOW or arm == ExperimentArm.CONTROL:
        return production_choice, "CONTROL"
    if not analysis.ranking:
        return None, "NO_CANDIDATE"
    if state.reward_mode == RewardMode.REVENUE and not state.revenue_data_ready:
        raise OptimizerSelectorError(
            "Revenue optimizer requested without a Revenue Provider"
        )
    return analysis.ranking[0].candidate, "OPTIMIZER"


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def engagement_proxy(
    views: Optional[int],
    replies: Optional[int],
    reposts: Optional[int],
    quotes: Optional[int],
    shares: Optional[int],
) -> float:
    """0-100 proxy: 60% discovery and 40% guarded deep engagement."""
    safe_views = max(0, int(views or 0))
    discovery = clamp(
        math.log1p(safe_views) / math.log1p(10_000) * 100.0,
        0.0,
        100.0,
    )
    if safe_views < DEEP_ENGAGEMENT_MIN_VIEWS:
        deep = 50.0
    else:
        actions = sum(value or 0 for value in (replies, reposts, quotes, shares))
        deep = clamp(actions / safe_views * 1000.0, 0.0, 100.0)
    return clamp(0.60 * discovery + 0.40 * deep, 0.0, 100.0)


def smooth_proxy(
    sample_mean: float,
    sample_count: int,
    global_mean: float,
    prior_strength: float = PRIOR_STRENGTH,
) -> float:
    return (
        sample_count * sample_mean + prior_strength * global_mean
    ) / (sample_count + prior_strength)


def historical_adjustment(deltas: Sequence[float]) -> float:
    if not deltas:
        return 0.0
    return clamp(
        0.10 * mean(deltas),
        -MAX_HISTORICAL_ADJUSTMENT,
        MAX_HISTORICAL_ADJUSTMENT,
    )


class PerformanceOptimizerSelector:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def load_outcomes(self, decided_at: datetime) -> List[PerformanceOutcome]:
        cutoff = decided_at.isoformat()
        lookback = (decided_at - timedelta(days=OPTIMIZER_LOOKBACK_DAYS)).isoformat()
        rows = self.connection.execute(
            """
            WITH eligible AS (
                SELECT tp.search_keyword category, tp.template_variant,
                       ps.views, ps.replies, ps.reposts, ps.quotes, ps.shares,
                       ROW_NUMBER() OVER (
                           PARTITION BY ps.threads_post_id
                           ORDER BY ps.captured_at ASC
                       ) position
                FROM post_performance_snapshots ps
                JOIN threads_posts tp
                  ON tp.threads_post_id = ps.threads_post_id
                WHERE tp.status = 'published'
                  AND ps.is_valid_72h = 1
                  AND ps.captured_at <= ?
                  AND ps.captured_at >= ?
            )
            SELECT * FROM eligible WHERE position = 1
            """,
            (cutoff, lookback),
        ).fetchall()
        return [
            PerformanceOutcome(
                category=row["category"],
                template_variant=row["template_variant"],
                proxy=engagement_proxy(
                    row["views"], row["replies"], row["reposts"],
                    row["quotes"], row["shares"],
                ),
            )
            for row in rows
        ]

    @staticmethod
    def _dimension_stats(
        outcomes: Sequence[PerformanceOutcome],
        field: str,
        global_mean: Optional[float],
        minimum_samples: int,
    ) -> Dict[str, DimensionStats]:
        grouped: Dict[str, List[float]] = {}
        for outcome in outcomes:
            key = getattr(outcome, field)
            if key:
                grouped.setdefault(key, []).append(outcome.proxy)
        result: Dict[str, DimensionStats] = {}
        for key, values in grouped.items():
            sample_mean = mean(values)
            smoothed = (
                smooth_proxy(sample_mean, len(values), global_mean)
                if global_mean is not None
                else None
            )
            result[key] = DimensionStats(
                n=len(values),
                mean_proxy=sample_mean,
                median_proxy=median(values),
                smoothed_proxy=smoothed,
                sufficient=len(values) >= minimum_samples,
            )
        return result

    def analyze(
        self,
        candidates: Sequence[ThreadsCandidate],
        *,
        decided_at: datetime,
        reward_mode: RewardMode,
    ) -> OptimizerAnalysis:
        outcomes = self.load_outcomes(decided_at)
        global_mean = mean(outcome.proxy for outcome in outcomes) if outcomes else None
        category_stats = self._dimension_stats(
            outcomes, "category", global_mean, MIN_CATEGORY_SAMPLES
        )
        template_stats = self._dimension_stats(
            outcomes, "template_variant", global_mean, MIN_TEMPLATE_SAMPLES
        )
        enabled = (
            reward_mode == RewardMode.ENGAGEMENT_PROXY
            and len(outcomes) >= MIN_72H_OUTCOMES
            and global_mean is not None
        )
        unsorted: List[OptimizerCandidateScore] = []
        for candidate in candidates:
            base = float(candidate.deal_score)
            if not math.isfinite(base) or not candidate.product.item_code:
                raise OptimizerSelectorError("Candidate contains an invalid base score or item code")
            category = category_stats.get(candidate.search_keyword)
            template = template_stats.get(candidate.template_variant)
            category_delta = (
                category.smoothed_proxy - global_mean
                if enabled and category and category.sufficient
                and category.smoothed_proxy is not None and global_mean is not None
                else None
            )
            template_delta = (
                template.smoothed_proxy - global_mean
                if enabled and template and template.sufficient
                and template.smoothed_proxy is not None and global_mean is not None
                else None
            )
            adjustment = historical_adjustment(
                [value for value in (category_delta, template_delta) if value is not None]
            )
            score = clamp(base + adjustment, 0.0, 100.0)
            if not math.isfinite(score):
                raise OptimizerSelectorError("Optimizer produced an invalid score")
            unsorted.append(
                OptimizerCandidateScore(
                    candidate=candidate,
                    candidate_source="DEAL",
                    base_quality_score=base,
                    global_mean_proxy=global_mean,
                    category_n=category.n if category else 0,
                    category_smoothed_proxy=category.smoothed_proxy if category else None,
                    category_delta=category_delta,
                    template_n=template.n if template else 0,
                    template_smoothed_proxy=template.smoothed_proxy if template else None,
                    template_delta=template_delta,
                    historical_adjustment=adjustment,
                    optimizer_score=score,
                    rank_position=0,
                )
            )
        ordered = sorted(
            unsorted,
            key=lambda item: (
                -item.optimizer_score,
                -item.base_quality_score,
                item.candidate.product.item_code,
            ),
        )
        ranking = [
            OptimizerCandidateScore(**{**item.__dict__, "rank_position": index})
            for index, item in enumerate(ordered, 1)
        ]
        return OptimizerAnalysis(
            valid_outcomes=len(outcomes),
            global_mean_proxy=global_mean,
            category_stats=category_stats,
            template_stats=template_stats,
            ranking=ranking,
            adjustment_enabled=enabled,
        )

    def persist_scores(
        self,
        analysis: OptimizerAnalysis,
        *,
        cycle_id: str,
        run_mode: str,
        decided_at: datetime,
        reward_mode: RewardMode,
        experiment_epoch: str,
        model_version: str = OPTIMIZER_MODEL_VERSION,
    ) -> None:
        timestamp = decided_at.isoformat()
        with self.connection:
            self.connection.executemany(
                """
                INSERT OR REPLACE INTO optimizer_shadow_candidate_scores(
                    cycle_id, run_mode, decided_at, training_data_cutoff,
                    item_code, category, template_variant, candidate_source,
                    base_quality_score, global_mean_proxy, category_n,
                    category_smoothed_proxy, category_delta, template_n,
                    template_smoothed_proxy, template_delta,
                    historical_adjustment, optimizer_score, reward_mode,
                    model_version, experiment_epoch, rank_position
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    (
                        cycle_id, run_mode, timestamp, timestamp,
                        item.candidate.product.item_code,
                        item.candidate.search_keyword,
                        item.candidate.template_variant,
                        item.candidate_source, item.base_quality_score,
                        item.global_mean_proxy, item.category_n,
                        item.category_smoothed_proxy, item.category_delta,
                        item.template_n, item.template_smoothed_proxy,
                        item.template_delta, item.historical_adjustment,
                        item.optimizer_score, reward_mode.value, model_version,
                        experiment_epoch, item.rank_position,
                    )
                    for item in analysis.ranking
                ),
            )

    def persist_decision(
        self,
        *,
        cycle_id: str,
        run_mode: str,
        decided_at: datetime,
        production_item_code: Optional[str],
        optimizer_choice: Optional[OptimizerCandidateScore],
        selected_item_code: Optional[str],
        reward_mode: RewardMode,
        experiment_epoch: str,
        experiment_arm: Optional[ExperimentArm],
        model_version: str = OPTIMIZER_MODEL_VERSION,
    ) -> None:
        if optimizer_choice is None:
            return
        category_contribution = optimizer_choice.category_delta
        template_contribution = optimizer_choice.template_delta
        reason = (
            f"Base Deal Score: {optimizer_choice.base_quality_score:.2f}; "
            f"Category adjustment source: n={optimizer_choice.category_n}; "
            f"Template adjustment source: n={optimizer_choice.template_n}; "
            f"Historical adjustment: {optimizer_choice.historical_adjustment:+.2f}; "
            f"Optimizer score: {optimizer_choice.optimizer_score:.2f}"
        )
        timestamp = decided_at.isoformat()
        with self.connection:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO optimizer_shadow_decisions(
                    cycle_id, run_mode, decided_at, training_data_cutoff,
                    production_item_code, optimizer_item_code, selected_item_code,
                    selected_base_score, selected_historical_adjustment,
                    selected_optimizer_score, category_n, template_n,
                    category_contribution, template_contribution, reason_summary,
                    reward_mode, model_version, experiment_epoch, experiment_arm
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    cycle_id, run_mode, timestamp, timestamp,
                    production_item_code,
                    optimizer_choice.candidate.product.item_code,
                    selected_item_code, optimizer_choice.base_quality_score,
                    optimizer_choice.historical_adjustment,
                    optimizer_choice.optimizer_score, optimizer_choice.category_n,
                    optimizer_choice.template_n, category_contribution,
                    template_contribution, reason, reward_mode.value,
                    model_version, experiment_epoch,
                    experiment_arm.value if experiment_arm else None,
                ),
            )
