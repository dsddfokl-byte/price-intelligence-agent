#!/usr/bin/env python3
"""Read-only safety and policy audit for bounded growth optimization."""

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import (  # noqa: E402
    AUTO_REPLY_MODE, DATABASE_PATH, GROWTH_OPTIMIZER_MODE, THREADS_PUBLISHING,
)
from app.database import Database  # noqa: E402
from app.growth_controller import (  # noqa: E402
    EXPERIMENT_QUEUES, assert_hard_constraints, bounded_auto_eligibility,
    current_policy_version, ensure_initial_policy,
)
from app.growth_optimizer import MUTATION_ALLOWLIST, MUTATION_DENYLIST, diagnose  # noqa: E402
from app.init import initialize_database  # noqa: E402


def main() -> int:
    now = datetime.now(timezone.utc)
    with Database(DATABASE_PATH) as database:
        initialize_database(database)
        ensure_initial_policy(database.connection, now)
        failures = []
        try:
            assert_hard_constraints()
        except RuntimeError as error:
            failures.append(str(error))
        active_policies = database.connection.execute(
            "SELECT COUNT(*) FROM growth_policies WHERE status='ACTIVE'"
        ).fetchone()[0]
        if active_policies != 1:
            failures.append("Exactly one ACTIVE growth policy is required")
        invalid_history = database.connection.execute(
            """
            SELECT COUNT(*) FROM growth_policy_history
            WHERE field_name IS NOT NULL AND field_name NOT IN ({})
            """.format(",".join("?" for _ in MUTATION_ALLOWLIST)),
            tuple(MUTATION_ALLOWLIST),
        ).fetchone()[0]
        if invalid_history:
            failures.append("Mutation history contains a non-allowlisted field")
        invalid_audits = database.connection.execute(
            "SELECT COUNT(*) FROM growth_decision_audits WHERE valid=0"
        ).fetchone()[0]
        recent_audits = database.connection.execute(
            "SELECT * FROM growth_decision_audits ORDER BY created_at DESC LIMIT 2"
        ).fetchall()
        for audit in recent_audits:
            try:
                evidence = json.loads(audit["evidence_json"])
            except (TypeError, json.JSONDecodeError):
                failures.append("Recent decision audit has invalid evidence JSON")
                continue
            if not audit["reason"] or not isinstance(evidence, dict):
                failures.append("Recent decision audit is not explainable")
        eligibility = bounded_auto_eligibility(database.connection)
        diagnosis = diagnose(database.connection, now)
        completed = eligibility.completed_experiments
        history = database.connection.execute(
            "SELECT action,from_policy,to_policy,field_name,reason,created_at FROM growth_policy_history ORDER BY id"
        ).fetchall()

        print(f"GROWTH_OPTIMIZER_MODE={GROWTH_OPTIMIZER_MODE}")
        print(f"BOUNDED_AUTO_ELIGIBLE={str(eligibility.eligible).lower()}")
        print(f"CURRENT_BOTTLENECK={diagnosis.current_bottleneck.value}")
        print(f"CURRENT_POLICY_VERSION={current_policy_version(database.connection)}")
        print(f"COMPLETED_EXPERIMENTS={completed}")
        print(f"INVALID_DECISION_AUDITS={invalid_audits}")
        print(f"SHADOW_DECISIONS_REVALIDATED={len(recent_audits)}")
        print(f"MUTATION_ALLOWLIST={','.join(sorted(MUTATION_ALLOWLIST))}")
        print(f"MUTATION_DENYLIST={','.join(sorted(MUTATION_DENYLIST))}")
        print(f"AUTO_REPLY_MODE={AUTO_REPLY_MODE}")
        print(f"DAILY_MAX_POSTS={THREADS_PUBLISHING.daily_post_limit}")
        print(f"CYCLE_MAX_POSTS={THREADS_PUBLISHING.cycle_post_limit}")
        print("ELIGIBILITY_REASONS=" + ("NONE" if not eligibility.reasons else " | ".join(eligibility.reasons)))
        print(f"POLICY_HISTORY_COUNT={len(history)}")
        for row in history:
            print("POLICY_HISTORY=" + json.dumps(dict(row), ensure_ascii=False, sort_keys=True))
        print(f"EXPERIMENT_QUEUE_STAGE={diagnosis.current_bottleneck.value}")
        print("EXPERIMENT_QUEUE=" + ",".join(EXPERIMENT_QUEUES[diagnosis.current_bottleneck]))
        print("SAFETY_AUDIT=" + ("PASS" if not failures else "FAIL"))
        for failure in failures:
            print(f"SAFETY_FAILURE={failure}")
        return 0 if not failures else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except sqlite3.Error:
        print("SAFETY_AUDIT=FAIL")
        sys.exit(1)
