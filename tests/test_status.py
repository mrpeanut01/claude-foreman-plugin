"""Status digest: what is in flight, what is stuck, and what needs a human."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ledger  # noqa: E402
import status  # noqa: E402


@pytest.fixture
def root(tmp_path):
    return ledger.init(tmp_path)


def test_empty_ledger_says_so(root):
    out = status.render(ledger.fold(ledger.read_events(root)), config={})
    assert "no batches" in out.lower()


def test_in_flight_batch_shows_state_and_gates(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[42, 43], pr=7)
    for s in ("building", "built", "open"):
        ledger.transition(root, "b-001", s)
    ledger.gate(root, "b-001", "ci", "cheap_green")
    out = status.render(ledger.fold(ledger.read_events(root)), config={})
    assert "b-001" in out and "open" in out
    assert "cheap_green" in out and "pending" in out
    assert "#42" in out and "#43" in out


def test_escalation_is_shown_with_its_reason(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    ledger.transition(root, "b-001", "building")
    ledger.transition(root, "b-001", "escalated")
    ledger.append(root, "escalation", batch="b-001", reason="same test failed 3 pushes running")
    out = status.render(ledger.fold(ledger.read_events(root)), config={})
    assert "same test failed 3 pushes running" in out


def test_batch_at_its_cap_is_flagged_as_needing_a_human(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    for s in ("building", "built", "open"):
        ledger.transition(root, "b-001", s)
    for sha in "abc":
        ledger.append(root, "batch.pushed", batch="b-001", sha=sha)
    out = status.render(ledger.fold(ledger.read_events(root)),
                        config={"caps": {"pushes": 3}})
    assert "pushes" in out
    assert "needs you" in out.lower()


def test_merged_batches_are_counted_not_listed(root):
    for i in (1, 2):
        bid = f"b-00{i}"
        ledger.append(root, "batch.created", batch=bid, issues=[i])
        for s in ("building", "built", "open"):
            ledger.transition(root, bid, s)
        ledger.gate(root, bid, "ci", "full_green")
        ledger.gate(root, bid, "review", "clean")
        for s in ("ready", "merging", "merged"):
            ledger.transition(root, bid, s)
    out = status.render(ledger.fold(ledger.read_events(root)), config={})
    assert "merged" in out.lower() and "2" in out


def test_flake_leaderboard_surfaces_the_worst_offender(root):
    for _ in range(4):
        ledger.append(root, "flake.observed", job="integration", test="test_login")
    ledger.append(root, "flake.observed", job="unit", test="test_parse")
    out = status.render(ledger.fold(ledger.read_events(root)), config={})
    assert "test_login" in out and "4" in out


def test_review_verdicts_and_post_merge_reverts_are_reported(root):
    """The rubber-stamp signal: clean reviews that later got reverted."""
    ledger.append(root, "review.verdict", batch="b-001", verdict="clean")
    ledger.append(root, "review.verdict", batch="b-002", verdict="clean")
    ledger.append(root, "merge.reverted", batch="b-002", within_days=2)
    out = status.render(ledger.fold(ledger.read_events(root)), config={})
    assert "1/2" in out or "50" in out
