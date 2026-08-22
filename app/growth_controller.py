"""Bounded, auditable growth-policy decisions without source-code mutation."""

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from app.config import (
    AUTO_REPLY_MODE,
    DISTRIBUTION_EXIT_MEDIAN_VIEWS,
    GROWTH_GUARDRAIL_MAX_DETERIORATION,
    GROWTH_MAX_DUPLICATE_RATE,
    GROWTH_MAX_PUBLISH_FAILURE_RATE,
    GROWTH_OPTIMIZER_MODE,
    GROWTH_PERSISTENT_LOW_EXPERIMENTS,
    GROWTH_POLICY_INITIAL_VERSION,
    GROWTH_ROLLBACK_MONITOR_DAYS,
    THREADS_PUBLISHING,
)
from app.growth_optimizer import (
    Bottleneck,
    EvaluationResult,
    ExperimentStatus,
    MUTATION_ALLOWLIST,
    MUTATION_DENYLIST,
    diagnose,
    evaluate_views_experiment,
)


class ControllerMode(str, Enum):
    SHADOW = "shadow"
    BOUNDED_AUTO = "bounded_auto"


EXPERIMENT_QUEUES: Mapping[Bottleneck, Tuple[str, ...]] = {
    Bottleneck.DISTRIBUTION: ("POST_INTENT", "TOPIC_RELEVANCE", "POSTING_SLOT", "MEDIA_VARIANT"),
    Bottleneck.ENGAGEMENT: ("QUESTION", "HOOK_TYPE", "CONTENT_LENGTH", "COMIC_THEME"),
    Bottleneck.CLICK: ("PRODUCT_FRAMING", "PRICE_CLARITY", "CTA"),
    Bottleneck.CONVERSION: ("DEAL_QUALITY", "CATEGORY", "PRICE_DROP_MAGNITUDE"),
    Bottleneck.MONETIZATION: ("COMMISSION_RATE", "ORDER_VALUE", "CATEGORY_MIX"),
    Bottleneck.STABLE: (),
}

EXPERIMENT_MUTATIONS = {
    "POST_INTENT": "post_intent_allocation",
    "TOPIC_RELEVANCE": "topic_tag_candidate",
    "POSTING_SLOT": "posting_slot_preference",
    "MEDIA_VARIANT": "comic_selection_weight",
    "QUESTION": "question_presence",
    "HOOK_TYPE": "hook_template",
    "CONTENT_LENGTH": "text_length_target",
    "COMIC_THEME": "comic_selection_weight",
    "PRODUCT_FRAMING": "hook_template",
    "PRICE_CLARITY": "hook_template",
    "CTA": "cta_template",
    "DEAL_QUALITY": "hook_template",
    "CATEGORY": "topic_tag_candidate",
    "PRICE_DROP_MAGNITUDE": "hook_template",
    "COMMISSION_RATE": "hook_template",
    "ORDER_VALUE": "hook_template",
    "CATEGORY_MIX": "topic_tag_candidate",
}

EXPERIMENT_ARMS = {
    "POST_INTENT": ("AFFILIATE", "GROWTH"),
    "TOPIC_RELEVANCE": ("BASELINE", "RELEVANCE_WEIGHT_V2"),
    "POSTING_SLOT": ("NEUTRAL", "PREFERRED_SLOT"),
    "MEDIA_VARIANT": ("NO_COMIC", "COMIC"),
    "QUESTION": ("NO_QUESTION", "QUESTION"),
    "HOOK_TYPE": ("BASELINE", "TREATMENT_HOOK"),
    "CONTENT_LENGTH": ("BASELINE", "TARGET_LENGTH"),
    "COMIC_THEME": ("BASELINE", "THEME_WEIGHT_V2"),
    "PRODUCT_FRAMING": ("BASELINE", "FRAMING_V2"),
    "PRICE_CLARITY": ("BASELINE", "PRICE_CLARITY_V2"),
    "CTA": ("BASELINE", "CTA_V2"),
    "DEAL_QUALITY": ("BASELINE", "DEAL_QUALITY_V2"),
    "CATEGORY": ("BASELINE", "CATEGORY_MIX_V2"),
    "PRICE_DROP_MAGNITUDE": ("BASELINE", "PRICE_DROP_V2"),
    "COMMISSION_RATE": ("BASELINE", "COMMISSION_RATE_V2"),
    "ORDER_VALUE": ("BASELINE", "ORDER_VALUE_V2"),
    "CATEGORY_MIX": ("BASELINE", "CATEGORY_MIX_V2"),
}

STAGE_OBJECTIVES = {
    Bottleneck.DISTRIBUTION: "median_views_per_post",
    Bottleneck.ENGAGEMENT: "replies_per_view",
    Bottleneck.CLICK: "affiliate_clicks_per_post",
    Bottleneck.CONVERSION: "orders_per_click",
    Bottleneck.MONETIZATION: "commission_per_day",
    Bottleneck.STABLE: "commission_per_day",
}


@dataclass(frozen=True)
class GuardrailMetrics:
    commission_per_day: Optional[float]
    orders_per_day: Optional[float]
    clicks_per_day: Optional[float]
    publish_failure_rate: float
    duplicate_rate: float

    def as_dict(self) -> Dict[str, Optional[float]]:
        return {
            "commission_per_day": self.commission_per_day,
            "orders_per_day": self.orders_per_day,
            "clicks_per_day": self.clicks_per_day,
            "publish_failure_rate": self.publish_failure_rate,
            "duplicate_rate": self.duplicate_rate,
        }


@dataclass(frozen=True)
class GuardrailResult:
    acceptable: bool
    reason: str


@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reasons: Tuple[str, ...]
    completed_experiments: int


@dataclass(frozen=True)
class ControllerDecision:
    mode: str
    bottleneck: Bottleneck
    active_experiment: Optional[str]
    next_experiment: Optional[str]
    evaluation: Optional[EvaluationResult]
    guardrails: Optional[GuardrailResult]
    mutation_applied: bool
    persistent_low: bool
    reason: str


@dataclass(frozen=True)
class ExperimentDecision:
    status: ExperimentStatus
    decision: str
    evaluation: EvaluationResult
    guardrails: GuardrailResult
    reason: str


def default_policy_values() -> Dict[str, Any]:
    return {
        "post_intent_allocation": "50/50",
        "topic_tag_candidate": "relevance_v1",
        "hook_template": "baseline",
        "question_presence": "deterministic",
        "text_length_target": 500,
        "comic_selection_weight": "baseline",
        "posting_slot_preference": "neutral",
        "cta_template": "baseline",
    }


def assert_hard_constraints() -> None:
    if THREADS_PUBLISHING.daily_post_limit > 4:
        raise RuntimeError("Denylist violation: daily post limit")
    if THREADS_PUBLISHING.cycle_post_limit > 1:
        raise RuntimeError("Denylist violation: cycle post limit")
    if THREADS_PUBLISHING.minimum_deal_score != 75.0:
        raise RuntimeError("Denylist violation: Deal Score threshold")
    if AUTO_REPLY_MODE != "shadow":
        raise RuntimeError("Denylist violation: auto reply mode")


def validate_mutation(field_name: str, new_value: Any) -> None:
    assert_hard_constraints()
    if field_name in MUTATION_DENYLIST or field_name not in MUTATION_ALLOWLIST:
        raise ValueError(f"Mutation is not allowlisted: {field_name}")
    if field_name == "text_length_target" and not 1 <= int(new_value) <= 500:
        raise ValueError("Text length target must remain within Threads limits")
    if field_name == "topic_tag_candidate" and str(new_value) == "disable_relevance_threshold":
        raise ValueError("Topic relevance safety cannot be disabled")


def ensure_initial_policy(connection: sqlite3.Connection, now: datetime) -> str:
    with connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO growth_policies(
                policy_version, status, values_json, reason, created_at, activated_at
            ) VALUES (?, 'ACTIVE', ?, 'Initial bounded policy baseline', ?, ?)
            """,
            (
                GROWTH_POLICY_INITIAL_VERSION,
                json.dumps(default_policy_values(), sort_keys=True),
                now.isoformat(),
                now.isoformat(),
            ),
        )
    return current_policy_version(connection)


def current_policy_version(connection: sqlite3.Connection) -> str:
    row = connection.execute(
        "SELECT policy_version FROM growth_policies WHERE status='ACTIVE' ORDER BY activated_at DESC LIMIT 1"
    ).fetchone()
    return str(row[0]) if row else GROWTH_POLICY_INITIAL_VERSION


def next_policy_version(connection: sqlite3.Connection) -> str:
    versions = [
        int(str(row[0]).rsplit("v", 1)[1])
        for row in connection.execute("SELECT policy_version FROM growth_policies")
        if str(row[0]).startswith("growth_policy_v") and str(row[0]).rsplit("v", 1)[1].isdigit()
    ]
    return f"growth_policy_v{max(versions or [0]) + 1}"


def guardrails_acceptable(baseline: GuardrailMetrics, current: GuardrailMetrics) -> GuardrailResult:
    if current.publish_failure_rate > GROWTH_MAX_PUBLISH_FAILURE_RATE:
        return GuardrailResult(False, "Publish failure rate exceeded")
    if current.duplicate_rate > GROWTH_MAX_DUPLICATE_RATE:
        return GuardrailResult(False, "Duplicate rate exceeded")
    for name in ("commission_per_day", "orders_per_day", "clicks_per_day"):
        before, after = getattr(baseline, name), getattr(current, name)
        if before is not None and before > 0 and after is not None:
            if after < before * (1.0 - GROWTH_GUARDRAIL_MAX_DETERIORATION):
                return GuardrailResult(False, f"{name} materially deteriorated")
    return GuardrailResult(True, "No material account-level guardrail deterioration")


def decide_experiment(
    control: Sequence[float], treatment: Sequence[float], runtime_days: float,
    baseline: GuardrailMetrics, current: GuardrailMetrics,
) -> ExperimentDecision:
    evaluation = evaluate_views_experiment(control, treatment, runtime_days)
    guardrails = guardrails_acceptable(baseline, current)
    if evaluation.status == ExperimentStatus.ADOPT and guardrails.acceptable:
        return ExperimentDecision(
            ExperimentStatus.ADOPT, "ADOPT", evaluation, guardrails,
            f"Robust uplift={evaluation.relative_uplift:.3f}; {guardrails.reason}",
        )
    if evaluation.status == ExperimentStatus.ADOPT and not guardrails.acceptable:
        return ExperimentDecision(
            ExperimentStatus.REJECT, "REJECT", evaluation, guardrails,
            f"Performance uplift rejected by guardrail: {guardrails.reason}",
        )
    return ExperimentDecision(
        evaluation.status, evaluation.decision, evaluation, guardrails,
        f"{evaluation.reason}; {guardrails.reason}",
    )


def metrics_for_period(connection: sqlite3.Connection, start: datetime, end: datetime) -> GuardrailMetrics:
    days = max((end - start).total_seconds() / 86400.0, 1.0)
    row = connection.execute(
        """
        SELECT SUM(clicks), SUM(confirmed_orders), SUM(confirmed_commission),
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END), COUNT(*)
        FROM threads_posts WHERE posted_at>=? AND posted_at<?
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchone()
    total = int(row[4] or 0)
    duplicate_count = connection.execute(
        """
        SELECT COUNT(*) FROM threads_posts newer JOIN threads_posts older
          ON newer.text_hash=older.text_hash AND newer.id>older.id
        WHERE newer.status='published' AND older.status='published'
          AND newer.posted_at>=? AND newer.posted_at<?
          AND julianday(newer.posted_at)-julianday(older.posted_at)<30
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchone()[0]
    return GuardrailMetrics(
        float(row[2]) / days if row[2] is not None else None,
        float(row[1]) / days if row[1] is not None else None,
        float(row[0]) / days if row[0] is not None else None,
        float(row[3] or 0) / total if total else 0.0,
        float(duplicate_count) / total if total else 0.0,
    )


def decision_audit_valid(experiment: Mapping[str, Any], evaluation: EvaluationResult, guardrails: GuardrailResult) -> Tuple[bool, str]:
    if evaluation.status == ExperimentStatus.INSUFFICIENT_SAMPLE:
        return False, "Minimum sample/runtime not reached"
    if evaluation.decision == "ADOPT_CANDIDATE" and not guardrails.acceptable:
        return False, "Adoption conflicts with guardrails"
    if not experiment.get("decision_reason") and experiment.get("decision"):
        return False, "Decision reason is missing"
    return True, "Decision evidence is internally consistent"


def record_decision_audit(
    connection: sqlite3.Connection,
    experiment_id: str,
    evaluation: EvaluationResult,
    guardrails: GuardrailResult,
    valid: bool,
    reason: str,
    now: datetime,
) -> str:
    audit_id = str(uuid.uuid4())
    evidence = {
        "control_n": evaluation.control_n,
        "treatment_n": evaluation.treatment_n,
        "relative_uplift": evaluation.relative_uplift,
        "bootstrap_ci": evaluation.bootstrap_ci,
        "guardrails": guardrails.reason,
    }
    connection.execute(
        "INSERT INTO growth_decision_audits VALUES (?, ?, ?, ?, ?, ?, ?)",
        (audit_id, experiment_id, evaluation.decision, int(valid), reason,
         json.dumps(evidence, sort_keys=True), now.isoformat()),
    )
    return audit_id


def completed_experiment_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute(
        "SELECT COUNT(*) FROM growth_experiments WHERE status IN ('ADOPT','REJECT','INCONCLUSIVE')"
    ).fetchone()[0])


def bounded_auto_eligibility(connection: sqlite3.Connection) -> EligibilityResult:
    reasons = []
    integrity = connection.execute("PRAGMA quick_check").fetchone()
    if integrity is None or integrity[0] != "ok":
        reasons.append("Database integrity check failed")
    active_policies = int(connection.execute(
        "SELECT COUNT(*) FROM growth_policies WHERE status='ACTIVE'"
    ).fetchone()[0])
    if active_policies != 1:
        reasons.append("Growth policy state is inconsistent")
    completed = completed_experiment_count(connection)
    if completed < 1:
        reasons.append("No completed experiment")
    invalid_audits = int(connection.execute(
        "SELECT COUNT(*) FROM growth_decision_audits WHERE valid=0"
    ).fetchone()[0])
    valid_audits = int(connection.execute(
        "SELECT COUNT(*) FROM growth_decision_audits WHERE valid=1"
    ).fetchone()[0])
    if invalid_audits or valid_audits < completed:
        reasons.append("Decision audit is incomplete or invalid")
    placeholders = ",".join("?" for _ in MUTATION_ALLOWLIST)
    invalid_mutations = int(connection.execute(
        f"SELECT COUNT(*) FROM growth_policy_history WHERE field_name IS NOT NULL AND field_name NOT IN ({placeholders})",
        tuple(MUTATION_ALLOWLIST),
    ).fetchone()[0])
    if invalid_mutations:
        reasons.append("Hard-constraint mutation exists in policy history")
    try:
        assert_hard_constraints()
    except RuntimeError as error:
        reasons.append(str(error))
    duplicate_incidents = int(connection.execute(
        """
        SELECT COUNT(*) FROM threads_posts newer JOIN threads_posts older
          ON newer.text_hash=older.text_hash AND newer.id>older.id
        WHERE newer.status='published' AND older.status='published'
          AND newer.experiment_arm='OPTIMIZER'
          AND julianday(newer.posted_at)-julianday(older.posted_at)<30
        """
    ).fetchone()[0])
    if duplicate_incidents:
        reasons.append("Optimizer-caused duplicate incident")
    return EligibilityResult(not reasons, tuple(reasons), completed)


def select_next_experiment(
    bottleneck: Bottleneck,
    completed_families: Sequence[str],
    active_experiment: Optional[str],
) -> Optional[str]:
    if active_experiment:
        return None
    completed = set(completed_families)
    return next((family for family in EXPERIMENT_QUEUES[bottleneck] if family not in completed), None)


def start_next_experiment(
    connection: sqlite3.Connection, family: str, bottleneck: Bottleneck,
    now: datetime,
) -> str:
    if family not in EXPERIMENT_MUTATIONS or family not in EXPERIMENT_ARMS:
        raise ValueError("Unknown bounded experiment family")
    control, treatment = EXPERIMENT_ARMS[family]
    identifier = f"{family.lower()}-{uuid.uuid4().hex}"
    connection.execute("BEGIN IMMEDIATE")
    try:
        active = connection.execute(
            """
            SELECT COUNT(*) FROM growth_experiments
            WHERE status IN ('PLANNED','RUNNING','INSUFFICIENT_SAMPLE','READY_FOR_EVALUATION')
            """
        ).fetchone()[0]
        if active:
            raise RuntimeError("A primary growth experiment is already active")
        connection.execute(
            """
            INSERT INTO growth_experiments(
                experiment_id,experiment_family,epoch,status,control_arm,
                treatment_arm,started_at,primary_metric,minimum_samples_per_arm,
                minimum_runtime_days,created_at
            ) VALUES (?,?,?,'RUNNING',?,?,?,?,20,7,?)
            """,
            (identifier, family, f"bounded-{family.lower()}-v1", control,
             treatment, now.isoformat(), STAGE_OBJECTIVES[bottleneck], now.isoformat()),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return identifier


def persistent_distribution_low(
    bottleneck: Bottleneck, completed_distribution_experiments: int,
    median_views_14d: Optional[float],
) -> bool:
    return (
        bottleneck == Bottleneck.DISTRIBUTION
        and completed_distribution_experiments >= GROWTH_PERSISTENT_LOW_EXPERIMENTS
        and (median_views_14d is None or median_views_14d < DISTRIBUTION_EXIT_MEDIAN_VIEWS)
    )


def apply_policy_mutation(
    connection: sqlite3.Connection,
    *, field_name: str, new_value: Any, experiment_id: str, reason: str,
    sample_size: int, effect_size: float, baseline_metrics: GuardrailMetrics,
    now: datetime,
) -> str:
    validate_mutation(field_name, new_value)
    current = current_policy_version(connection)
    row = connection.execute(
        "SELECT values_json FROM growth_policies WHERE policy_version=?", (current,)
    ).fetchone()
    values = json.loads(row[0]) if row else default_policy_values()
    previous = values.get(field_name)
    values[field_name] = new_value
    target = next_policy_version(connection)
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "UPDATE growth_policies SET status='STABLE', deactivated_at=? WHERE status='ACTIVE'",
            (now.isoformat(),),
        )
        connection.execute(
            """
            INSERT INTO growth_policies VALUES (?, 'ACTIVE', ?, ?, ?, ?, ?, ?, NULL)
            """,
            (target, json.dumps(values, sort_keys=True), experiment_id, reason,
             json.dumps(baseline_metrics.as_dict(), sort_keys=True), now.isoformat(), now.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO growth_policy_history(
                action, from_policy, to_policy, field_name, previous_value,
                new_value, experiment_id, reason, sample_size, effect_size,
                metrics_json, created_at
            ) VALUES ('ADOPT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (current, target, field_name, json.dumps(previous), json.dumps(new_value),
             experiment_id, reason, sample_size, effect_size,
             json.dumps(baseline_metrics.as_dict(), sort_keys=True), now.isoformat()),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return target


def rollback_policy(
    connection: sqlite3.Connection, reason: str,
    metrics: GuardrailMetrics, now: datetime,
) -> str:
    current = current_policy_version(connection)
    current_row = connection.execute(
        "SELECT activated_at FROM growth_policies WHERE policy_version=?", (current,)
    ).fetchone()
    if current_row and datetime.fromisoformat(current_row[0]) > now - timedelta(days=GROWTH_ROLLBACK_MONITOR_DAYS):
        raise RuntimeError("Rollback monitoring window has not completed")
    previous = connection.execute(
        "SELECT policy_version FROM growth_policies WHERE status='STABLE' ORDER BY deactivated_at DESC LIMIT 1"
    ).fetchone()
    if previous is None:
        raise RuntimeError("No stable policy is available for rollback")
    target = str(previous[0])
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "UPDATE growth_policies SET status='ROLLED_BACK', deactivated_at=? WHERE policy_version=?",
            (now.isoformat(), current),
        )
        connection.execute(
            "UPDATE growth_policies SET status='ACTIVE', activated_at=?, deactivated_at=NULL WHERE policy_version=?",
            (now.isoformat(), target),
        )
        payload = json.dumps(metrics.as_dict(), sort_keys=True)
        connection.execute(
            "INSERT INTO growth_rollbacks(from_policy,to_policy,reason,metrics_json,created_at) VALUES (?,?,?,?,?)",
            (current, target, reason, payload, now.isoformat()),
        )
        connection.execute(
            "INSERT INTO growth_policy_history(action,from_policy,to_policy,reason,metrics_json,created_at) VALUES ('ROLLBACK',?,?,?,?,?)",
            (current, target, reason, payload, now.isoformat()),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    return target


def maybe_rollback_policy(
    connection: sqlite3.Connection, now: datetime
) -> Optional[str]:
    row = connection.execute(
        """
        SELECT policy_version, activated_at, baseline_metrics_json
        FROM growth_policies WHERE status='ACTIVE' AND adopted_experiment_id IS NOT NULL
        ORDER BY activated_at DESC LIMIT 1
        """
    ).fetchone()
    if row is None or not row["activated_at"] or not row["baseline_metrics_json"]:
        return None
    activated = datetime.fromisoformat(row["activated_at"])
    if activated > now - timedelta(days=GROWTH_ROLLBACK_MONITOR_DAYS):
        return None
    baseline = GuardrailMetrics(**json.loads(row["baseline_metrics_json"]))
    current = metrics_for_period(
        connection, now - timedelta(days=GROWTH_ROLLBACK_MONITOR_DAYS), now
    )
    result = guardrails_acceptable(baseline, current)
    if result.acceptable:
        return None
    return rollback_policy(connection, result.reason, current, now)


class BoundedGrowthController:
    def __init__(self, connection: sqlite3.Connection, mode: str = GROWTH_OPTIMIZER_MODE) -> None:
        self.connection = connection
        self.mode = ControllerMode(mode)

    def evaluate(self, now: Optional[datetime] = None) -> ControllerDecision:
        current = now or datetime.now(timezone.utc)
        diagnosis = diagnose(self.connection, current)
        active = self.connection.execute(
            """
            SELECT * FROM growth_experiments
            WHERE status IN ('PLANNED','RUNNING','INSUFFICIENT_SAMPLE','READY_FOR_EVALUATION')
            ORDER BY created_at LIMIT 1
            """
        ).fetchone()
        completed_rows = self.connection.execute(
            "SELECT experiment_family FROM growth_experiments WHERE status IN ('ADOPT','REJECT','INCONCLUSIVE')"
        ).fetchall()
        completed = [str(row[0]) for row in completed_rows]
        next_experiment = select_next_experiment(
            diagnosis.current_bottleneck, completed,
            str(active["experiment_id"]) if active else None,
        )
        distribution_completed = sum(
            family in EXPERIMENT_QUEUES[Bottleneck.DISTRIBUTION] for family in completed
        )
        persistent = persistent_distribution_low(
            diagnosis.current_bottleneck, distribution_completed,
            diagnosis.windows[14].distribution.median,
        )
        if persistent:
            next_experiment = None
        persistent_reason = (
            "DISTRIBUTION_PERSISTENT_LOW; profile positioning, account history, "
            "follower base, or market fit may contribute but no cause is asserted"
        )
        experiment_decision = None
        baseline_guardrails = current_guardrails = None
        if active and active["started_at"] and active["experiment_family"] == "POST_INTENT":
            started = datetime.fromisoformat(active["started_at"])
            runtime_days = max(0.0, (current - started).total_seconds() / 86400.0)
            rows = self.connection.execute(
                """
                SELECT post_intent, views FROM threads_posts
                WHERE status='published' AND posted_at>=? AND posted_at<=?
                  AND post_intent IN ('AFFILIATE','GROWTH') AND views IS NOT NULL
                """,
                (started.isoformat(), current.isoformat()),
            ).fetchall()
            control = [float(row["views"]) for row in rows if row["post_intent"] == "AFFILIATE"]
            treatment = [float(row["views"]) for row in rows if row["post_intent"] == "GROWTH"]
            duration = max(current - started, timedelta(days=1))
            baseline_guardrails = metrics_for_period(self.connection, started - duration, started)
            current_guardrails = metrics_for_period(self.connection, started, current)
            experiment_decision = decide_experiment(
                control, treatment, runtime_days, baseline_guardrails, current_guardrails
            )
        if self.mode == ControllerMode.SHADOW:
            return ControllerDecision(
                self.mode.value, diagnosis.current_bottleneck,
                str(active["experiment_id"]) if active else None,
                next_experiment,
                experiment_decision.evaluation if experiment_decision else None,
                experiment_decision.guardrails if experiment_decision else None,
                False, persistent,
                persistent_reason if persistent else
                "Shadow mode: hypothetical decision only; no policy or experiment mutation",
            )
        eligibility = bounded_auto_eligibility(self.connection)
        if not eligibility.eligible:
            return ControllerDecision(
                self.mode.value, diagnosis.current_bottleneck,
                str(active["experiment_id"]) if active else None,
                None, None, None, False, persistent,
                "Bounded auto eligibility failed: " + "; ".join(eligibility.reasons),
            )
        rolled_back_to = maybe_rollback_policy(self.connection, current)
        if rolled_back_to:
            return ControllerDecision(
                self.mode.value, diagnosis.current_bottleneck,
                str(active["experiment_id"]) if active else None,
                None, experiment_decision.evaluation if experiment_decision else None,
                experiment_decision.guardrails if experiment_decision else None,
                True, persistent, f"Rolled back to {rolled_back_to}",
            )
        if active is None and next_experiment and not persistent:
            started_id = start_next_experiment(
                self.connection, next_experiment, diagnosis.current_bottleneck, current
            )
            return ControllerDecision(
                self.mode.value, diagnosis.current_bottleneck, started_id,
                None, None, None, False, persistent,
                f"Started one bounded experiment: {next_experiment}",
            )
        mutation_applied = False
        reason = "Eligible for bounded decisions; active experiment is not ready"
        if active and experiment_decision and experiment_decision.status != ExperimentStatus.INSUFFICIENT_SAMPLE:
            experiment_map = dict(active)
            experiment_map["decision"] = experiment_decision.decision
            experiment_map["decision_reason"] = experiment_decision.reason
            valid, audit_reason = decision_audit_valid(
                experiment_map, experiment_decision.evaluation, experiment_decision.guardrails
            )
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                self.connection.execute(
                    "UPDATE growth_experiments SET status=?, ended_at=?, decision=?, decision_reason=? WHERE experiment_id=?",
                    (experiment_decision.status.value, current.isoformat(),
                     experiment_decision.decision, experiment_decision.reason,
                     active["experiment_id"]),
                )
                record_decision_audit(
                    self.connection, str(active["experiment_id"]),
                    experiment_decision.evaluation, experiment_decision.guardrails,
                    valid, audit_reason, current,
                )
                self.connection.commit()
            except Exception:
                self.connection.rollback()
                raise
            if experiment_decision.status == ExperimentStatus.ADOPT and valid:
                assert baseline_guardrails is not None
                field = EXPERIMENT_MUTATIONS[str(active["experiment_family"])]
                apply_policy_mutation(
                    self.connection, field_name=field,
                    new_value=f"adopted_{active['treatment_arm']}",
                    experiment_id=str(active["experiment_id"]),
                    reason=experiment_decision.reason,
                    sample_size=(experiment_decision.evaluation.control_n
                                 + experiment_decision.evaluation.treatment_n),
                    effect_size=float(experiment_decision.evaluation.relative_uplift or 0),
                    baseline_metrics=baseline_guardrails, now=current,
                )
                mutation_applied = True
            reason = experiment_decision.reason
        return ControllerDecision(
            self.mode.value, diagnosis.current_bottleneck,
            str(active["experiment_id"]) if active else None,
            next_experiment,
            experiment_decision.evaluation if experiment_decision else None,
            experiment_decision.guardrails if experiment_decision else None,
            mutation_applied, persistent, reason,
        )
