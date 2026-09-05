"""End to end: issues in one side, a merge decision out the other.

Every stage is the real module. Only GitHub is absent.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import batch  # noqa: E402
import land  # noqa: E402
import ledger  # noqa: E402
import loop  # noqa: E402
import triage  # noqa: E402

CONFIG = {
    "auto_merge": True,
    "caps": {"pushes": 3, "review_rounds": 2, "reruns": 2},
    "limits": {
        "max_open_prs": 3,
        "max_batch_issues": 3,
        "max_batch_weight": 6,
        "max_ci_minutes_per_day": 400,
    },
    "risk_ceiling": "medium",
    "protected_paths": ["**/auth/**", "**/migrations/**"],
}

LABELS = ["bug", "enhancement", "question", "duplicate", "needs-repro", "needs-info"]

ISSUES = [
    {
        "number": 1,
        "title": "Upload retries forever on 503",
        "body": "Traceback in src/upload.py line 88 shows the retry loop never exits.",
        "labels": ["bug"],
        "state": "open",
        "updatedAt": "2026-09-01T00:00:00Z",
    },
    {
        "number": 2,
        "title": "Parser drops trailing comma",
        "body": "Run `foo parse a,b,` in src/parser/lexer.py and the last field vanishes.",
        "labels": ["bug"],
        "state": "open",
        "updatedAt": "2026-09-01T00:00:00Z",
    },
    {
        "number": 3,
        "title": "Session token never expires",
        "body": "Old tokens in src/auth/session.py stay valid forever.",
        "labels": ["bug"],
        "state": "open",
        "updatedAt": "2026-09-01T00:00:00Z",
    },
    {
        "number": 4,
        "title": "It is broken",
        "body": "Does not work.",
        "labels": ["bug"],
        "state": "open",
        "updatedAt": "2026-09-01T00:00:00Z",
    },
    {
        "number": 5,
        "title": "Upload retries forever on 503 errors",
        "body": "Same as the other one.",
        "labels": ["bug"],
        "state": "open",
        "updatedAt": "2026-09-01T00:00:00Z",
    },
]


@pytest.fixture
def triaged():
    records = [triage.triage_issue(i, ISSUES, CONFIG["protected_paths"], LABELS) for i in ISSUES]
    # paths come from the issue text; batching needs them to prove independence
    for record, issue in zip(records, ISSUES, strict=True):
        record["paths"] = triage._paths_in(issue["body"])
    return records


def test_triage_separates_work_from_everything_else(triaged):
    by_number = {r["issue"]: r for r in triaged}
    assert by_number[1]["verdict"] == "actionable"
    assert by_number[2]["verdict"] == "actionable"
    assert by_number[3]["risk"] == "high", "an auth path must not be scored low"
    assert by_number[4]["verdict"] == "needs-repro", "no evidence, no expectation"
    assert by_number[5]["verdict"] == "duplicate" and by_number[5]["duplicate_of"] == 1


def test_batching_keeps_the_risky_issue_on_its_own(triaged):
    batches = batch.group_issues(triaged, CONFIG)
    grouped = {b["id"]: b["issues"] for b in batches}
    assert [3] in grouped.values(), "the auth issue must be alone"
    for issues in grouped.values():
        assert not (len(issues) > 1 and 3 in issues)
    queued = sorted(i for issues in grouped.values() for i in issues)
    assert queued == [1, 2, 3], "only actionable issues; no duplicate, no needs-repro"


def test_savings_are_reported_against_a_real_profile(triaged):
    batches = batch.group_issues(triaged, CONFIG)
    saved = batch.estimate_savings(batches, {"cheap_tier_s": 120, "expensive_tier_s": 2280})
    assert saved["issues"] == 3
    assert saved["seconds_saved"] == (3 - len(batches)) * 2400


def test_the_loop_walks_a_batch_from_planned_to_merged(tmp_path, triaged):
    root = ledger.init(tmp_path)
    for record in triaged:
        ledger.append(root, "issue.triaged", **record)
    batches = batch.group_issues(triaged, CONFIG)
    work = next(b for b in batches if 3 not in b["issues"])
    ledger.append(
        root, "batch.created", batch=work["id"], issues=work["issues"], paths=work["paths"]
    )

    def act():
        return loop.next_action(ledger.load(root), CONFIG)

    assert act()["do"] == "build"
    ledger.transition(root, work["id"], "building")
    ledger.transition(root, work["id"], "built")
    assert act()["do"] == "open_pr"

    ledger.transition(root, work["id"], "open")
    ledger.append(root, "batch.pushed", batch=work["id"], sha="abc123")
    assert act()["do"] == "watch"

    # cheap CI green, review still out: the expensive tier has not been earned
    ledger.gate(root, work["id"], "ci", "cheap_green")
    assert not ledger.may_run_expensive_tier(ledger.load(root).batches[work["id"]])

    ledger.gate(root, work["id"], "review", "clean")
    assert ledger.may_run_expensive_tier(ledger.load(root).batches[work["id"]])

    ledger.gate(root, work["id"], "ci", "full_green")
    assert act()["do"] == "advance"
    ledger.transition(root, work["id"], "ready")

    action = act()
    assert action["do"] == "merge" and action["batch"] == work["id"]
    ledger.transition(root, work["id"], "merging")
    ledger.transition(root, work["id"], "merged")
    assert act()["do"] in {"idle", "batch"}


def test_the_auth_batch_reaches_ready_and_then_refuses_to_merge(tmp_path, triaged):
    """Green is not sufficient. A protected path escalates instead."""
    root = ledger.init(tmp_path)
    risky = next(b for b in batch.group_issues(triaged, CONFIG) if b["issues"] == [3])
    ledger.append(root, "batch.created", batch=risky["id"], issues=[3], paths=risky["paths"])
    for step in ("building", "built", "open"):
        ledger.transition(root, risky["id"], step)
    ledger.gate(root, risky["id"], "ci", "full_green")
    ledger.gate(root, risky["id"], "review", "clean")
    ledger.transition(root, risky["id"], "ready")

    action = loop.next_action(ledger.load(root), CONFIG)
    assert action["do"] == "escalate"
    assert "auth" in action["reason"]


def test_a_review_verdict_without_evidence_never_becomes_a_clean_gate():
    ok, errors = land.validate_review(
        {
            "verdict": "clean",
            "tests_covering": [],
            "revert_check": "not_applicable",
            "findings": [],
            "behaviour_change": True,
        }
    )
    assert not ok and len(errors) == 2
    assert not any(e.startswith("verdict must be") for e in errors)
