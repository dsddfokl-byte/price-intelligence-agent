#!/usr/bin/env python3
"""Run one collection-to-Threads publishing cycle."""

import argparse
import logging
import sqlite3
import sys
import os
import uuid
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.collector import collect_products, load_search_terms  # noqa: E402
from app.comic_cycle import build_comic_plan, publish_with_comic_plan  # noqa: E402
from app.comics.media_publisher import ComicThreadsPublisher  # noqa: E402
from app.comics.stock_selector import COMIC, ComicUsageRecord  # noqa: E402
from app.autopilot import (  # noqa: E402
    AutopilotController,
    ExecutionState,
    ExperimentArm,
    HaltClass,
    StateValidationError,
    get_state,
    emergency_safe_halt,
    new_controller_evaluation_id,
    record_experiment_assignment,
    record_publish_result,
    runtime_policy,
    stable_arm_assignment,
    system_health_errors,
    validate_publish_payload,
)
from app.config import (  # noqa: E402
    AUTOMATION_LOG_PATH,
    COMIC_MEDIA_EXPERIMENT_EPOCH,
    ConfigurationError,
    RUN_CYCLE_LOCK_PATH,
    SEARCH_TERMS_PATH,
    THREADS_PUBLISHING,
    load_settings,
    load_threads_access_token,
)
from app.database import Database  # noqa: E402
from app.init import initialize_database  # noqa: E402
from app.publishers.threads import (  # noqa: E402
    ThreadsAPIError,
    ThreadsPostError,
    daily_period_start,
    find_eligible_candidates,
)
from app.optimizer_selector import (  # noqa: E402
    OPTIMIZER_MODEL_VERSION,
    PerformanceOptimizerSelector,
    choose_candidate_for_arm,
)
from app.rakuten_client import RakutenAPIError, RakutenClient  # noqa: E402
from app.run_lock import CycleLock, LockAlreadyHeld  # noqa: E402


LOGGER = logging.getLogger("affiliate_automation")


def configure_logging() -> None:
    AUTOMATION_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        AUTOMATION_LOG_PATH,
        maxBytes=1_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    LOGGER.propagate = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one affiliate automation cycle")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collect and preview without publishing to Threads",
    )
    parser.add_argument(
        "--no-collect",
        action="store_true",
        help="select a candidate from the existing database",
    )
    return parser.parse_args()


def run_cycle(args: argparse.Namespace) -> int:
    LOGGER.info(
        "Cycle started dry_run=%s no_collect=%s",
        args.dry_run,
        args.no_collect,
    )
    lock = CycleLock(RUN_CYCLE_LOCK_PATH)
    try:
        lock.acquire()
    except LockAlreadyHeld as error:
        LOGGER.info("Cycle skipped reason=%s", error)
        LOGGER.info("Cycle finished status=skipped")
        return 0
    except OSError:
        LOGGER.error("Cycle failed lock_error")
        LOGGER.info("Cycle finished status=failed")
        return 1

    try:
        try:
            settings = load_settings()
            search_terms = (
                [] if args.no_collect else load_search_terms(SEARCH_TERMS_PATH)
            )
        except (ConfigurationError, RuntimeError) as error:
            LOGGER.error("Cycle failed configuration_error=%s", error)
            return 1

        try:
            with Database(settings.database_path) as database:
                initialize_database(database)

                if not args.no_collect:
                    with RakutenClient(settings) as client:
                        result = collect_products(
                            database,
                            client,
                            search_terms,
                            LOGGER,
                            hits=30,
                        )
                    LOGGER.info("API fetched item_count=%d", result.fetched_count)
                    LOGGER.info("Products updated item_count=%d", result.updated_count)
                else:
                    LOGGER.info("Collection skipped reason=--no-collect")

                now = datetime.now(timezone.utc)
                controller = AutopilotController(database.connection)
                health_errors = system_health_errors(
                    database.connection,
                    now,
                    THREADS_PUBLISHING.daily_post_limit,
                )
                if health_errors:
                    controller.evaluate(
                        new_controller_evaluation_id(),
                        now=now,
                        global_safety_error="; ".join(health_errors),
                        halt_class=HaltClass.MANUAL_REVIEW_REQUIRED,
                    )
                try:
                    state = get_state(database.connection)
                except StateValidationError:
                    state = emergency_safe_halt(
                        database.connection,
                        new_controller_evaluation_id(),
                        "State machine corruption",
                        now=now,
                    )
                if os.getenv("AUTOPILOT_SAFE_HALT", "").lower() in (
                    "1", "true", "yes"
                ) and state.execution_state != ExecutionState.SAFE_HALT:
                    state = controller.evaluate(
                        new_controller_evaluation_id(),
                        now=now,
                        global_safety_error="Human SAFE_HALT runtime override",
                        halt_class=HaltClass.MANUAL_REVIEW_REQUIRED,
                    )
                if (
                    state.execution_state == ExecutionState.SAFE_HALT
                    and os.getenv("AUTOPILOT_CLEAR_SAFE_HALT", "").lower()
                    in ("1", "true", "yes")
                ):
                    state = controller.evaluate(
                        new_controller_evaluation_id(),
                        now=now,
                        clear_safe_halt=True,
                    )
                if state.optimizer_model_version != OPTIMIZER_MODEL_VERSION:
                    state = controller.evaluate(
                        new_controller_evaluation_id(),
                        now=now,
                        optimizer_model_version=OPTIMIZER_MODEL_VERSION,
                    )
                policy = runtime_policy(state)
                if policy.halt_publish:
                    LOGGER.warning("Post skipped reason=%s", policy.reason)
                    return 0

                day_start = daily_period_start(
                    now,
                    THREADS_PUBLISHING.daily_timezone,
                )
                posted_today = database.published_threads_count_since(
                    day_start.isoformat()
                )
                if posted_today > THREADS_PUBLISHING.daily_post_limit:
                    controller.evaluate(
                        new_controller_evaluation_id(),
                        now=now,
                        global_safety_error="Posting limit violation",
                        halt_class=HaltClass.MANUAL_REVIEW_REQUIRED,
                    )
                    LOGGER.critical("Post blocked reason=posting_limit_violation")
                    return 1
                if posted_today >= THREADS_PUBLISHING.daily_post_limit:
                    LOGGER.info(
                        "Post skipped reason=daily_limit posted_count=%d limit=%d timezone=%s",
                        posted_today,
                        THREADS_PUBLISHING.daily_post_limit,
                        THREADS_PUBLISHING.daily_timezone,
                    )
                    return 0

                cycle_id = str(uuid.uuid4())
                assigned_arm, assignment_key = stable_arm_assignment(
                    state.experiment_epoch, cycle_id, state.execution_state
                )
                selector_used = "CONTROL"
                formal_experiment = state.execution_state in (
                    ExecutionState.LIMITED_LIVE,
                    ExecutionState.ADAPTIVE_LIVE,
                ) and not args.dry_run
                if policy.force_control:
                    assigned_arm = ExperimentArm.CONTROL
                    formal_experiment = False
                candidates = find_eligible_candidates(
                    database, THREADS_PUBLISHING, now
                )
                LOGGER.info("Candidates selected candidate_count=%d", len(candidates))
                if not candidates:
                    LOGGER.info("Post skipped reason=no_publishable_candidate")
                    return 0

                production_candidate = candidates[0]
                optimizer = PerformanceOptimizerSelector(database.connection)
                if args.dry_run:
                    run_mode = "dry_run"
                elif state.execution_state == ExecutionState.SHADOW:
                    run_mode = "shadow"
                else:
                    run_mode = f"{state.execution_state.value.lower()}_{assigned_arm.value.lower()}"
                optimizer_choice = None
                try:
                    analysis = optimizer.analyze(
                        candidates,
                        decided_at=now,
                        reward_mode=state.reward_mode,
                    )
                    optimizer.persist_scores(
                        analysis,
                        cycle_id=cycle_id,
                        run_mode=run_mode,
                        decided_at=now,
                        reward_mode=state.reward_mode,
                        experiment_epoch=state.experiment_epoch,
                    )
                    optimizer_choice = analysis.ranking[0] if analysis.ranking else None
                    candidate, selected_by = choose_candidate_for_arm(
                        candidates, analysis, state, assigned_arm
                    )
                    selector_used = selected_by
                except sqlite3.Error:
                    raise
                except Exception as error:
                    state = controller.evaluate(
                        new_controller_evaluation_id(),
                        now=now,
                        optimizer_error=type(error).__name__,
                    )
                    selector_used = "FALLBACK_CONTROL"
                    formal_experiment = False
                    candidate = production_candidate
                if optimizer_choice is not None:
                    optimizer.persist_decision(
                        cycle_id=cycle_id,
                        run_mode=run_mode,
                        decided_at=now,
                        production_item_code=production_candidate.product.item_code,
                        optimizer_choice=optimizer_choice,
                        selected_item_code=candidate.product.item_code,
                        reward_mode=state.reward_mode,
                        experiment_epoch=state.experiment_epoch,
                        experiment_arm=assigned_arm,
                    )
                try:
                    validate_publish_payload(
                        candidate.text, candidate.product.affiliate_url
                    )
                except StateValidationError as error:
                    controller.evaluate(
                        new_controller_evaluation_id(),
                        now=now,
                        global_safety_error=str(error),
                        halt_class=HaltClass.MANUAL_REVIEW_REQUIRED,
                    )
                    LOGGER.critical("Post blocked reason=payload_safety_validation")
                    return 1
                if not args.dry_run:
                    record_experiment_assignment(
                        database.connection,
                        cycle_id=cycle_id,
                        state=state,
                        arm=assigned_arm,
                        assignment_key=assignment_key,
                        assigned_at=now,
                        selector_used=selector_used,
                        selected_item_code=candidate.product.item_code,
                        candidate_score=candidate.deal_score,
                        formal_override=formal_experiment,
                    )
                LOGGER.info(
                    "Post target item_code=%s deal_score=%.2f",
                    candidate.product.item_code,
                    candidate.deal_score,
                )
                usages = tuple(
                    ComicUsageRecord(
                        comic_id=row["comic_id"],
                        item_code=row["item_code"],
                        category=row["category"],
                        selected_at=datetime.fromisoformat(row["selected_at"]),
                        published_at=(
                            datetime.fromisoformat(row["published_at"])
                            if row["published_at"] else None
                        ),
                    )
                    for row in database.comic_usage_rows()
                )
                comic_plan = build_comic_plan(candidate, now=now, usages=usages)

                if args.dry_run:
                    LOGGER.info("Post skipped reason=dry_run")
                    print("DRY RUN: Threadsへは投稿しません")
                    print(f"item_code: {candidate.product.item_code}")
                    print(f"deal_score: {candidate.deal_score:.2f}")
                    print(
                        "production_selector: "
                        f"{production_candidate.product.item_code}"
                    )
                    if optimizer_choice is not None:
                        print(
                            "optimizer_selector: "
                            f"{optimizer_choice.candidate.product.item_code}"
                        )
                        print(
                            "optimizer_adjustment: "
                            f"{optimizer_choice.historical_adjustment:+.2f}"
                        )
                        print(
                            "optimizer_score: "
                            f"{optimizer_choice.optimizer_score:.2f}"
                        )
                    print(f"topic_tag: {candidate.topic_tag}")
                    print(f"template_variant: {candidate.template_variant}")
                    print(f"tip_id: {candidate.tip_id or 'N/A'}")
                    print(f"content_trigger: {candidate.content_trigger or 'N/A'}")
                    print(
                        "assigned_media_variant: "
                        f"{comic_plan.assigned_media_variant}"
                    )
                    print(f"comic_id: {comic_plan.selection.comic_id or 'N/A'}")
                    print(candidate.text)
                    return 0

                try:
                    record_publish_result(database.connection, cycle_id, "attempting")
                    token = load_threads_access_token()
                    with ComicThreadsPublisher(token) as publisher:
                        media_outcome = publish_with_comic_plan(
                            publisher, candidate, comic_plan
                        )
                    post_id = media_outcome.post_id
                except (ConfigurationError, ThreadsAPIError, ThreadsPostError) as error:
                    selection = comic_plan.selection
                    database.record_threads_post(
                        item_code=candidate.product.item_code,
                        threads_post_id=None,
                        posted_at=now.isoformat(),
                        deal_score=candidate.deal_score,
                        price=candidate.product.item_price,
                        text_hash=candidate.text_hash,
                        status="failed",
                        error=str(error),
                        topic_tag=candidate.topic_tag,
                        template_variant=candidate.template_variant,
                        tip_id=candidate.tip_id,
                        content_trigger=candidate.content_trigger,
                        search_keyword=candidate.search_keyword,
                        experiment_arm=assigned_arm.value,
                        assignment_key=assignment_key,
                        experiment_epoch=state.experiment_epoch,
                        comic_id=selection.comic_id,
                        comic_file=(
                            selection.file_path.name if selection.file_path else None
                        ),
                        comic_stock_version=selection.stock_version,
                        assigned_media_variant=comic_plan.assigned_media_variant,
                        delivered_media_variant="NO_COMIC",
                        comic_media_experiment_epoch=COMIC_MEDIA_EXPERIMENT_EPOCH,
                    )
                    record_publish_result(database.connection, cycle_id, "failed")
                    LOGGER.error(
                        "Threads publish failed item_code=%s error=%s",
                        candidate.product.item_code,
                        error,
                    )
                    return 1

                selection = media_outcome.selection
                if media_outcome.delivered_media_variant == COMIC:
                    assert selection.comic_id and selection.file_path
                    assert media_outcome.hosted is not None
                    database.record_published_comic_post(
                        item_code=candidate.product.item_code,
                        threads_post_id=post_id,
                        posted_at=now.isoformat(),
                        deal_score=candidate.deal_score,
                        price=candidate.product.item_price,
                        text_hash=candidate.text_hash,
                        topic_tag=candidate.topic_tag,
                        template_variant=candidate.template_variant,
                        tip_id=candidate.tip_id,
                        content_trigger=candidate.content_trigger,
                        search_keyword=candidate.search_keyword,
                        comic_id=selection.comic_id,
                        comic_file=selection.file_path.name,
                        comic_stock_version=selection.stock_version,
                        media_url=media_outcome.hosted.public_url,
                        media_hosting_provider=media_outcome.hosted.provider,
                        selected_at=now.isoformat(),
                        selection_score=selection.selection_score,
                        selection_reason=selection.selection_reason,
                        experiment_arm=assigned_arm.value,
                        assignment_key=assignment_key,
                        experiment_epoch=state.experiment_epoch,
                        comic_media_experiment_epoch=COMIC_MEDIA_EXPERIMENT_EPOCH,
                    )
                else:
                    database.record_threads_post(
                        item_code=candidate.product.item_code,
                        threads_post_id=post_id,
                        posted_at=now.isoformat(),
                        deal_score=candidate.deal_score,
                        price=candidate.product.item_price,
                        text_hash=candidate.text_hash,
                        status="published",
                        topic_tag=candidate.topic_tag,
                        template_variant=candidate.template_variant,
                        tip_id=candidate.tip_id,
                        content_trigger=candidate.content_trigger,
                        search_keyword=candidate.search_keyword,
                        experiment_arm=assigned_arm.value,
                        assignment_key=assignment_key,
                        experiment_epoch=state.experiment_epoch,
                        comic_id=selection.comic_id,
                        comic_file=(
                            selection.file_path.name if selection.file_path else None
                        ),
                        comic_stock_version=selection.stock_version,
                        assigned_media_variant=media_outcome.assigned_media_variant,
                        delivered_media_variant=media_outcome.delivered_media_variant,
                        comic_media_experiment_epoch=COMIC_MEDIA_EXPERIMENT_EPOCH,
                        comic_fallback_reason=media_outcome.fallback_reason,
                    )
                record_publish_result(database.connection, cycle_id, "published")
                LOGGER.info(
                    "Threads publish succeeded item_code=%s deal_score=%.2f post_id=%s",
                    candidate.product.item_code,
                    candidate.deal_score,
                    post_id,
                )
                return 0
        except RakutenAPIError as error:
            LOGGER.error("Cycle failed rakuten_api_error=%s", error)
            return 1
        except sqlite3.Error:
            LOGGER.error("Cycle failed database_error")
            return 1
        except Exception as error:
            LOGGER.error("Cycle failed unexpected_error_type=%s", type(error).__name__)
            return 1
    finally:
        lock.release()
        LOGGER.info("Cycle finished")


def main() -> int:
    configure_logging()
    return run_cycle(parse_args())


if __name__ == "__main__":
    sys.exit(main())
