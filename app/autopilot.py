"""Safety-first execution and reward control for the publishing autopilot."""

import hashlib
import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from statistics import mean, median
from typing import List, Optional, Protocol, Sequence, Tuple
from zoneinfo import ZoneInfo

from app.post_text import (
    AFFILIATE_DISCLOSURE,
    has_valid_affiliate_disclosure_layout,
)


EXPERIMENT_SEED = "rakuten-affiliate-agent-experiment-v1"
DEEP_ENGAGEMENT_MIN_VIEWS = 50
MIN_MATURE_OUTCOMES_PER_ARM = 20
MIN_PROMOTION_DWELL = timedelta(hours=24)
MIN_EVALUATION_INTERVAL = timedelta(hours=24)

PRIMARY_REVENUE_METRIC = "commission_per_published_post"
SECONDARY_REVENUE_METRICS = (
    "commission_per_1000_views",
    "conversion_rate",
    "CTR",
    "views",
)
PRIMARY_CLICK_METRIC = "clicks_per_published_post"
SECONDARY_CLICK_METRIC = "clicks_per_1000_views"


class ExecutionState(str, Enum):
    SHADOW = "SHADOW"
    LIMITED_LIVE = "LIMITED_LIVE"
    ADAPTIVE_LIVE = "ADAPTIVE_LIVE"
    FALLBACK_CONTROL = "FALLBACK_CONTROL"
    SAFE_HALT = "SAFE_HALT"


class RewardMode(str, Enum):
    ENGAGEMENT_PROXY = "ENGAGEMENT_PROXY"
    CLICK_PROXY = "CLICK_PROXY"
    REVENUE = "REVENUE"


class ExperimentArm(str, Enum):
    CONTROL = "CONTROL"
    OPTIMIZER = "OPTIMIZER"


class OutcomeStatus(str, Enum):
    MATURE = "MATURE"
    IMMATURE = "IMMATURE"
    UNKNOWN = "UNKNOWN"


class HaltClass(str, Enum):
    TRANSIENT = "TRANSIENT"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"


class StateValidationError(RuntimeError):
    """Raised for a forbidden execution/reward state combination."""


class RevenueProvider(Protocol):
    """Future provider contract; no provider is configured yet."""

    def get_outcome_maturity_window(self) -> timedelta:
        ...


@dataclass(frozen=True)
class AutopilotState:
    execution_state: ExecutionState
    reward_mode: RewardMode
    experiment_epoch: str
    optimizer_model_version: str
    controller_version: str
    scoring_formula_version: str
    experiment_policy_version: str
    revenue_data_ready: bool
    safe_halt_reason: Optional[str]
    safe_halt_class: Optional[HaltClass]
    manual_clear_required: bool
    health_success_count: int
    healthy_since: Optional[datetime]
    state_entered_at: datetime
    updated_at: datetime
    promotion_success_count: int = 0
    last_promotion_eval_at: Optional[datetime] = None


@dataclass(frozen=True)
class RewardComparison:
    control_n: int
    optimizer_n: int
    control_mean: float
    optimizer_mean: float
    control_median: float
    optimizer_median: float
    control_trimmed_mean: float
    optimizer_trimmed_mean: float
    probability_optimizer_better: float
    relative_uplift: float
    robustly_better: bool


@dataclass(frozen=True)
class RuntimePolicy:
    halt_publish: bool
    force_control: bool
    reason: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def maximum_execution_state(reward_mode: RewardMode) -> ExecutionState:
    if reward_mode == RewardMode.REVENUE:
        return ExecutionState.ADAPTIVE_LIVE
    return ExecutionState.LIMITED_LIVE


def validate_state_combination(
    execution_state: ExecutionState, reward_mode: RewardMode
) -> None:
    if execution_state == ExecutionState.ADAPTIVE_LIVE and reward_mode != RewardMode.REVENUE:
        raise StateValidationError(
            f"{execution_state.value} requires reward mode REVENUE"
        )


def stable_arm_assignment(
    experiment_epoch: str,
    cycle_id: str,
    execution_state: ExecutionState,
    seed: str = EXPERIMENT_SEED,
) -> Tuple[ExperimentArm, str]:
    assignment_key = f"{experiment_epoch}|{cycle_id}|{seed}"
    bucket = int.from_bytes(
        hashlib.sha256(assignment_key.encode("utf-8")).digest()[:8], "big"
    ) % 100
    if execution_state == ExecutionState.LIMITED_LIVE:
        arm = ExperimentArm.OPTIMIZER if bucket < 50 else ExperimentArm.CONTROL
    elif execution_state == ExecutionState.ADAPTIVE_LIVE:
        arm = ExperimentArm.OPTIMIZER if bucket < 90 else ExperimentArm.CONTROL
    else:
        arm = ExperimentArm.CONTROL
    return arm, assignment_key


def deep_engagement_reward(
    views: Optional[int],
    replies: Optional[int],
    reposts: Optional[int],
    quotes: Optional[int],
    shares: Optional[int],
) -> Optional[float]:
    if views is None or views < DEEP_ENGAGEMENT_MIN_VIEWS:
        return None
    deep_actions = sum(value or 0 for value in (replies, reposts, quotes, shares))
    return deep_actions / views


def outcome_status(
    posted_at: datetime,
    now: datetime,
    maturity_window: Optional[timedelta],
) -> OutcomeStatus:
    if maturity_window is None:
        return OutcomeStatus.UNKNOWN
    if now < posted_at + maturity_window:
        return OutcomeStatus.IMMATURE
    return OutcomeStatus.MATURE


def _trimmed_mean(values: Sequence[float], proportion: float = 0.10) -> float:
    ordered = sorted(values)
    trim = int(len(ordered) * proportion)
    retained = ordered[trim : len(ordered) - trim] if trim else ordered
    return mean(retained)


def compare_rewards(
    control: Sequence[float], optimizer: Sequence[float]
) -> RewardComparison:
    if not control or not optimizer:
        return RewardComparison(
            len(control), len(optimizer), 0, 0, 0, 0, 0, 0, 0, 0, False
        )
    control_mean = mean(control)
    optimizer_mean = mean(optimizer)
    control_median = median(control)
    optimizer_median = median(optimizer)
    control_trimmed = _trimmed_mean(control)
    optimizer_trimmed = _trimmed_mean(optimizer)
    pair_count = len(control) * len(optimizer)
    probability = sum(o > c for o in optimizer for c in control) / pair_count
    relative_uplift = (
        (optimizer_mean - control_mean) / control_mean
        if control_mean > 0
        else (1.0 if optimizer_mean > 0 else 0.0)
    )
    robust = (
        optimizer_mean > control_mean
        and optimizer_median > control_median
        and optimizer_trimmed > control_trimmed
    )
    return RewardComparison(
        len(control),
        len(optimizer),
        control_mean,
        optimizer_mean,
        control_median,
        optimizer_median,
        control_trimmed,
        optimizer_trimmed,
        probability,
        relative_uplift,
        robust,
    )


def state_from_row(row: sqlite3.Row) -> AutopilotState:
    halt_class = row["safe_halt_class"]
    return AutopilotState(
        execution_state=ExecutionState(row["execution_state"]),
        reward_mode=RewardMode(row["reward_mode"]),
        experiment_epoch=row["experiment_epoch"],
        optimizer_model_version=row["optimizer_model_version"],
        controller_version=row["controller_version"],
        scoring_formula_version=row["scoring_formula_version"],
        experiment_policy_version=row["experiment_policy_version"],
        revenue_data_ready=bool(row["revenue_data_ready"]),
        safe_halt_reason=row["safe_halt_reason"],
        safe_halt_class=HaltClass(halt_class) if halt_class else None,
        manual_clear_required=bool(row["manual_clear_required"]),
        health_success_count=int(row["health_success_count"]),
        healthy_since=_parse_time(row["healthy_since"]),
        state_entered_at=datetime.fromisoformat(row["state_entered_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        promotion_success_count=int(row["promotion_success_count"]),
        last_promotion_eval_at=_parse_time(row["last_promotion_eval_at"]),
    )


def get_state(connection: sqlite3.Connection) -> AutopilotState:
    row = connection.execute("SELECT * FROM autopilot_state WHERE id = 1").fetchone()
    if row is None:
        raise StateValidationError("Autopilot state is missing")
    state = state_from_row(row)
    validate_state_combination(state.execution_state, state.reward_mode)
    return state


def new_experiment_epoch() -> str:
    return f"epoch-{uuid.uuid4().hex}"


def runtime_policy(
    state: AutopilotState, environ: Optional[dict] = None
) -> RuntimePolicy:
    values = environ if environ is not None else os.environ
    if state.execution_state == ExecutionState.SAFE_HALT or values.get(
        "AUTOPILOT_SAFE_HALT", ""
    ).lower() in ("1", "true", "yes"):
        return RuntimePolicy(True, True, "SAFE_HALT")
    if values.get("AUTOPILOT_FORCE_CONTROL", "").lower() in ("1", "true", "yes"):
        return RuntimePolicy(False, True, "AUTOPILOT_FORCE_CONTROL")
    if values.get("AUTOPILOT_ENABLED", "true").lower() in ("0", "false", "no"):
        return RuntimePolicy(True, True, "AUTOPILOT_DISABLED")
    if state.execution_state == ExecutionState.FALLBACK_CONTROL:
        return RuntimePolicy(False, True, "FALLBACK_CONTROL")
    return RuntimePolicy(False, False, state.execution_state.value)


def quick_integrity_check(connection: sqlite3.Connection) -> bool:
    row = connection.execute("PRAGMA quick_check").fetchone()
    return row is not None and row[0] == "ok"


def system_health_errors(
    connection: sqlite3.Connection,
    now: Optional[datetime] = None,
    daily_limit: int = 2,
) -> List[str]:
    """Return global hard-safety violations, separate from API availability."""
    errors: List[str] = []
    if not quick_integrity_check(connection):
        errors.append("DB integrity failure")
    required_columns = {
        "threads_posts": {
            "item_code", "posted_at", "text_hash", "status", "price",
            "experiment_arm", "assignment_key", "experiment_epoch",
        },
        "autopilot_state": {
            "execution_state", "reward_mode", "experiment_epoch",
            "manual_clear_required",
        },
    }
    for table, required in required_columns.items():
        columns = {
            row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if not required <= columns:
            errors.append(f"Major schema inconsistency: {table}")

    current_time = now or utc_now()
    local_midnight = current_time.astimezone(ZoneInfo("Asia/Tokyo")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    since = local_midnight.astimezone(timezone.utc).isoformat()
    published_today = connection.execute(
        "SELECT COUNT(*) FROM threads_posts WHERE status='published' AND posted_at>=?",
        (since,),
    ).fetchone()[0]
    if published_today > daily_limit:
        errors.append("Posting limit violation")

    duplicate_text = connection.execute(
        """
        SELECT 1 FROM threads_posts newer
        JOIN threads_posts older
          ON newer.text_hash = older.text_hash AND newer.id > older.id
        WHERE newer.status='published' AND older.status='published'
          AND julianday(newer.posted_at) - julianday(older.posted_at) < 30
        LIMIT 1
        """
    ).fetchone()
    if duplicate_text is not None:
        errors.append("Duplicate text control violation")

    duplicate_item = connection.execute(
        """
        SELECT 1 FROM threads_posts newer
        JOIN threads_posts older
          ON newer.item_code = older.item_code AND newer.id > older.id
        WHERE newer.status='published' AND older.status='published'
          AND julianday(newer.posted_at) - julianday(older.posted_at) < 7
          AND NOT (
              older.price IS NOT NULL AND older.price > 0
              AND newer.price IS NOT NULL AND newer.price <= older.price * 0.90
          )
        LIMIT 1
        """
    ).fetchone()
    if duplicate_item is not None:
        errors.append("Duplicate item control violation")
    return errors


def emergency_safe_halt(
    connection: sqlite3.Connection,
    controller_evaluation_id: str,
    reason: str,
    *,
    now: Optional[datetime] = None,
) -> AutopilotState:
    """Repair an unreadable state row into a persistent SAFE_HALT atomically."""
    current_time = now or utc_now()
    timestamp = current_time.isoformat()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT * FROM autopilot_state WHERE id=1").fetchone()
        old_execution = row["execution_state"] if row else "CORRUPT_OR_MISSING"
        old_reward = row["reward_mode"] if row else RewardMode.ENGAGEMENT_PROXY.value
        try:
            reward = RewardMode(old_reward)
        except ValueError:
            reward = RewardMode.ENGAGEMENT_PROXY
        epoch = row["experiment_epoch"] if row and row["experiment_epoch"] else new_experiment_epoch()
        if row is None:
            raise StateValidationError("Autopilot state row is missing and cannot be repaired")
        connection.execute(
            """
            UPDATE autopilot_state SET execution_state='SAFE_HALT', reward_mode=?,
                safe_halt_reason=?, safe_halt_class='MANUAL_REVIEW_REQUIRED',
                manual_clear_required=1, health_success_count=0, healthy_since=NULL,
                state_entered_at=?, updated_at=? WHERE id=1
            """,
            (reward.value, reason, timestamp, timestamp),
        )
        connection.execute(
            "INSERT OR IGNORE INTO controller_evaluations VALUES (?,?,?,?,?,?)",
            (
                controller_evaluation_id, timestamp, ExecutionState.SAFE_HALT.value,
                reward.value, epoch, "EMERGENCY_SAFE_HALT",
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO autopilot_transitions(
                controller_evaluation_id, occurred_at, from_execution_state,
                to_execution_state, from_reward_mode, to_reward_mode,
                experiment_epoch, reason
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                controller_evaluation_id, timestamp, old_execution,
                ExecutionState.SAFE_HALT.value, old_reward, reward.value, epoch,
                "STATE_MACHINE_CORRUPTION",
            ),
        )
        connection.commit()
        return get_state(connection)
    except Exception:
        connection.rollback()
        raise


def record_experiment_assignment(
    connection: sqlite3.Connection,
    *,
    cycle_id: str,
    state: AutopilotState,
    arm: ExperimentArm,
    assignment_key: str,
    assigned_at: datetime,
    selector_used: str,
    selected_item_code: str,
    candidate_score: float,
    formal_override: Optional[bool] = None,
) -> None:
    formal = state.execution_state in (
        ExecutionState.LIMITED_LIVE,
        ExecutionState.ADAPTIVE_LIVE,
    )
    if formal_override is not None:
        formal = formal_override
    with connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO experiment_cycles(
                cycle_id, experiment_epoch, experiment_arm, assignment_key,
                reward_mode, assigned_at, selector_used, selected_item_code,
                candidate_score, decision, is_formal_experiment
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'SELECTED', ?)
            """,
            (
                cycle_id, state.experiment_epoch, arm.value, assignment_key,
                state.reward_mode.value, assigned_at.isoformat(), selector_used,
                selected_item_code, candidate_score, int(formal),
            ),
        )


def record_publish_result(
    connection: sqlite3.Connection, cycle_id: str, status: str
) -> None:
    with connection:
        connection.execute(
            """
            UPDATE experiment_cycles
            SET publish_attempted = 1, publish_status = ?
            WHERE cycle_id = ?
            """,
            (status, cycle_id),
        )


def validate_publish_payload(text: str, affiliate_url: Optional[str]) -> None:
    if not has_valid_affiliate_disclosure_layout(text):
        raise StateValidationError("Affiliate disclosure validation failure")
    if not affiliate_url or affiliate_url not in text:
        raise StateValidationError("Affiliate URL validation failure")
    if text.rfind(affiliate_url) > text.rfind(AFFILIATE_DISCLOSURE):
        raise StateValidationError("Affiliate URL must precede disclosure")
    if len(text) > 500:
        raise StateValidationError("Threads text hard limit violation")


def causal_rewards(
    connection: sqlite3.Connection,
    experiment_epoch: str,
    reward_mode: RewardMode,
) -> Tuple[List[float], List[float], int]:
    rows = connection.execute(
        """
        SELECT tp.experiment_arm, tp.views, tp.replies, tp.reposts, tp.quotes,
               tp.shares, tp.clicks, tp.confirmed_commission, tp.outcome_status
        FROM threads_posts tp
        JOIN experiment_cycles ec
          ON ec.assignment_key = tp.assignment_key
         AND ec.experiment_epoch = tp.experiment_epoch
        WHERE tp.status = 'published'
          AND tp.experiment_epoch = ?
          AND tp.experiment_arm IN ('CONTROL', 'OPTIMIZER')
          AND ec.is_formal_experiment = 1
        """,
        (experiment_epoch,),
    ).fetchall()
    rewards = {ExperimentArm.CONTROL: [], ExperimentArm.OPTIMIZER: []}
    immature = 0
    for row in rows:
        if row["outcome_status"] != OutcomeStatus.MATURE.value:
            immature += 1
            continue
        reward: Optional[float]
        if reward_mode == RewardMode.REVENUE:
            reward = row["confirmed_commission"]
        elif reward_mode == RewardMode.CLICK_PROXY:
            reward = row["clicks"]
        else:
            reward = deep_engagement_reward(
                row["views"], row["replies"], row["reposts"], row["quotes"], row["shares"]
            )
        if reward is not None:
            rewards[ExperimentArm(row["experiment_arm"])].append(float(reward))
    return rewards[ExperimentArm.CONTROL], rewards[ExperimentArm.OPTIMIZER], immature


class AutopilotController:
    """Atomic controller; BEGIN IMMEDIATE replaces a separate controller lock."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def evaluate(
        self,
        controller_evaluation_id: str,
        *,
        now: Optional[datetime] = None,
        requested_state: Optional[ExecutionState] = None,
        observed_reward_mode: Optional[RewardMode] = None,
        optimizer_error: Optional[str] = None,
        global_safety_error: Optional[str] = None,
        halt_class: HaltClass = HaltClass.MANUAL_REVIEW_REQUIRED,
        health_check_passed: Optional[bool] = None,
        api_outage: bool = False,
        optimizer_model_version: Optional[str] = None,
        controller_version: Optional[str] = None,
        scoring_formula_version: Optional[str] = None,
        experiment_policy_version: Optional[str] = None,
        clear_safe_halt: bool = False,
        evidence: Optional[RewardComparison] = None,
        no_major_health_errors: bool = True,
    ) -> AutopilotState:
        current_time = now or utc_now()
        timestamp = current_time.isoformat()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute(
                "SELECT 1 FROM controller_evaluations WHERE controller_evaluation_id = ?",
                (controller_evaluation_id,),
            ).fetchone()
            if existing is not None:
                self.connection.rollback()
                return get_state(self.connection)
            old = get_state(self.connection)
            state = old.execution_state
            reward = observed_reward_mode or old.reward_mode
            epoch = old.experiment_epoch
            reason = "NO_CHANGE"
            state_entered = old.state_entered_at
            safe_reason = old.safe_halt_reason
            safe_class = old.safe_halt_class
            manual_clear = old.manual_clear_required
            health_count = old.health_success_count
            healthy_since = old.healthy_since
            promotion_count = old.promotion_success_count
            last_promotion = old.last_promotion_eval_at

            versions = {
                "optimizer_model_version": optimizer_model_version or old.optimizer_model_version,
                "controller_version": controller_version or old.controller_version,
                "scoring_formula_version": scoring_formula_version or old.scoring_formula_version,
                "experiment_policy_version": experiment_policy_version or old.experiment_policy_version,
            }
            epoch_changed = reward != old.reward_mode or any(
                versions[name] != getattr(old, name) for name in versions
            )
            if epoch_changed:
                epoch = new_experiment_epoch()
                promotion_count = 0
                last_promotion = None
                reason = "NEW_EXPERIMENT_EPOCH"

            if global_safety_error:
                state = ExecutionState.SAFE_HALT
                safe_reason = global_safety_error
                safe_class = halt_class
                manual_clear = halt_class == HaltClass.MANUAL_REVIEW_REQUIRED
                health_count = 0
                healthy_since = None
                reason = "GLOBAL_SAFETY_ERROR"
            elif optimizer_error:
                state = ExecutionState.FALLBACK_CONTROL
                reason = "OPTIMIZER_ERROR"
            elif old.execution_state == ExecutionState.SAFE_HALT:
                if old.manual_clear_required:
                    if clear_safe_halt:
                        state = ExecutionState.SHADOW
                        safe_reason = safe_class = None
                        manual_clear = False
                        reason = "MANUAL_SAFE_HALT_CLEAR"
                elif health_check_passed:
                    health_count += 1
                    healthy_since = healthy_since or current_time
                    if health_count >= 2 and current_time - healthy_since >= timedelta(hours=24):
                        state = ExecutionState.SHADOW
                        safe_reason = safe_class = None
                        reason = "TRANSIENT_SAFE_HALT_RECOVERED"
                elif health_check_passed is False:
                    health_count = 0
                    healthy_since = None
            elif not api_outage:
                if old.execution_state == ExecutionState.ADAPTIVE_LIVE and reward != RewardMode.REVENUE:
                    state = ExecutionState.LIMITED_LIVE
                    reason = "REWARD_SIGNAL_DOWNGRADE"
                elif requested_state == ExecutionState.ADAPTIVE_LIVE:
                    validate_state_combination(requested_state, reward)
                    dwell_ok = current_time - old.state_entered_at >= MIN_PROMOTION_DWELL
                    interval_ok = (
                        last_promotion is None
                        or current_time - last_promotion >= MIN_EVALUATION_INTERVAL
                    )
                    evidence_ok = (
                        evidence is not None
                        and evidence.control_n >= MIN_MATURE_OUTCOMES_PER_ARM
                        and evidence.optimizer_n >= MIN_MATURE_OUTCOMES_PER_ARM
                        and evidence.probability_optimizer_better >= 0.90
                        and evidence.relative_uplift >= 0.05
                        and evidence.robustly_better
                        and no_major_health_errors
                        and dwell_ok
                        and interval_ok
                    )
                    if evidence_ok:
                        promotion_count += 1
                        last_promotion = current_time
                        reason = "PROMOTION_EVIDENCE_PASS"
                        if promotion_count >= 2:
                            state = ExecutionState.ADAPTIVE_LIVE
                            reason = "PROMOTED_TO_ADAPTIVE_LIVE"
                    else:
                        promotion_count = 0
                elif requested_state is not None:
                    validate_state_combination(requested_state, reward)
                    state = requested_state
                    reason = "REQUESTED_STATE_CHANGE"

            validate_state_combination(state, reward)
            if state != old.execution_state:
                state_entered = current_time
            revenue_ready = reward == RewardMode.REVENUE
            self.connection.execute(
                """
                UPDATE autopilot_state SET execution_state=?, reward_mode=?,
                    experiment_epoch=?, optimizer_model_version=?, controller_version=?,
                    scoring_formula_version=?, experiment_policy_version=?,
                    revenue_data_ready=?, safe_halt_reason=?, safe_halt_class=?,
                    manual_clear_required=?, health_success_count=?, healthy_since=?,
                    state_entered_at=?, updated_at=?, promotion_success_count=?,
                    last_promotion_eval_at=? WHERE id=1
                """,
                (
                    state.value, reward.value, epoch,
                    versions["optimizer_model_version"], versions["controller_version"],
                    versions["scoring_formula_version"], versions["experiment_policy_version"],
                    int(revenue_ready), safe_reason,
                    safe_class.value if safe_class else None, int(manual_clear), health_count,
                    healthy_since.isoformat() if healthy_since else None,
                    state_entered.isoformat(), timestamp, promotion_count,
                    last_promotion.isoformat() if last_promotion else None,
                ),
            )
            self.connection.execute(
                """
                INSERT INTO controller_evaluations VALUES (?, ?, ?, ?, ?, ?)
                """,
                (controller_evaluation_id, timestamp, state.value, reward.value, epoch, reason),
            )
            if state != old.execution_state or reward != old.reward_mode or epoch != old.experiment_epoch:
                self.connection.execute(
                    """
                    INSERT INTO autopilot_transitions(
                        controller_evaluation_id, occurred_at, from_execution_state,
                        to_execution_state, from_reward_mode, to_reward_mode,
                        experiment_epoch, reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        controller_evaluation_id, timestamp, old.execution_state.value,
                        state.value, old.reward_mode.value, reward.value, epoch, reason,
                    ),
                )
            self.connection.commit()
            return get_state(self.connection)
        except Exception:
            self.connection.rollback()
            raise


def new_controller_evaluation_id() -> str:
    return str(uuid.uuid4())
