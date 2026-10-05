"""Bound repeated automatic repair using persisted draft diagnostics.

This never grants or denies a formal stage commit. A changed draft can always
be checked, and only an actually passing preflight clears the repair history.
The same-issue stop applies to the current failure, not resolved older issues.
"""
from __future__ import annotations

import hashlib
import json

MAX_SAME_ISSUE_FAILURES = 3
MAX_FAILED_ATTEMPTS = 8


def advance(previous: dict | None, result: dict, validator_fingerprint: str, *, source: str = "PREFLIGHT") -> dict:
    if source not in {"PREFLIGHT", "COMPILATION"}:
        raise ValueError("Unknown repair diagnostic source")
    previous = previous if isinstance(previous, dict) else {}
    # A different tool, validator or successful compile is not a passing full
    # preflight. Keep this revision's budget until that check actually passes.
    if source == "PREFLIGHT" and result.get("status") == "PASSED":
        previous = {}
    preflights = previous.get("failed_preflights", 0)
    compilations = previous.get("failed_compilations", 0)
    # Existing checkpoints contain only failed_preflights; retain their budget.
    failures = previous.get("failed_attempts", preflights + compilations)
    counts = dict(previous.get("issue_counts", {}))
    # Reads, checkpoint saves and successful compilation are not fresh failure
    # diagnostics. Preserve the last decision (also for legacy checkpoints).
    current_maximum = previous.get("current_issue_failure_count", max(counts.values(), default=0))
    if result.get("status") == "FAILED":
        failures += 1
        preflights += source == "PREFLIGHT"
        compilations += source == "COMPILATION"
        keys = set()
        for issue in result.get("issues") or []:
            if isinstance(issue, dict):
                # Do not use the full payload: unrelated edits must not reset
                # a repeatedly failing field. Messages and source values are
                # not persisted in the progress key.
                identity = [issue.get(key) for key in ("gate", "path", "reason_code")]
                keys.add(hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest())
        for key in sorted(keys):
            counts[key] = min(counts.get(key, 0) + 1, MAX_SAME_ISSUE_FAILURES)
        # Retain A's history when it disappears, but only this failure's issues
        # can trigger the per-issue stop. If A recurs, its old count still applies.
        current_maximum = max((counts[key] for key in keys), default=0)
        # A large structural failure is already bounded by the total budget.
        counts = dict(sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:64])
    maximum = max(counts.values(), default=0)
    stopped = current_maximum >= MAX_SAME_ISSUE_FAILURES or failures >= MAX_FAILED_ATTEMPTS
    return {
        "schema_version": 1, "validator_fingerprint": validator_fingerprint,
        "failed_attempts": failures, "max_failed_attempts": MAX_FAILED_ATTEMPTS,
        "failed_preflights": preflights, "max_failed_preflights": MAX_FAILED_ATTEMPTS,
        "failed_compilations": compilations,
        "issue_counts": counts, "max_same_issue_failures": MAX_SAME_ISSUE_FAILURES,
        "highest_issue_failure_count": maximum,
        "current_issue_failure_count": current_maximum,
        "automatic_continuation": "STOP" if stopped else "CONTINUE",
        "reason": "REPAIR_NOT_CONVERGING" if stopped else "WITHIN_REPAIR_BUDGET",
    }


def public_progress(progress: dict) -> dict:
    return {key: value for key, value in progress.items() if key not in {"issue_counts", "validator_fingerprint"}}
