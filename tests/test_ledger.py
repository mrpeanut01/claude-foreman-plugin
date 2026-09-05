"""Ledger: an append-only event log folded into current state."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ledger  # noqa: E402


@pytest.fixture
def root(tmp_path):
    return ledger.init(tmp_path)


# --- append / read round trip -------------------------------------------------

def test_append_then_read_round_trip(root):
    ledger.append(root, "issue.triaged", issue=42, verdict="actionable")
    events = ledger.read_events(root)
    assert len(events) == 1
    assert events[0]["type"] == "issue.triaged"
    assert events[0]["issue"] == 42
    assert events[0]["verdict"] == "actionable"
    assert events[0]["ts"].endswith("Z")


def test_appends_accumulate_in_order(root):
    for n in (1, 2, 3):
        ledger.append(root, "issue.triaged", issue=n, verdict="actionable")
    assert [e["issue"] for e in ledger.read_events(root)] == [1, 2, 3]


def test_corrupt_line_is_skipped_not_fatal(root):
    ledger.append(root, "issue.triaged", issue=1, verdict="actionable")
    (root / "events.jsonl").open("a").write("{ this is not json\n")
    ledger.append(root, "issue.triaged", issue=2, verdict="actionable")
    events = ledger.read_events(root)
    assert [e["issue"] for e in events] == [1, 2]


# --- folding ------------------------------------------------------------------

def test_fold_builds_batch_from_events(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[42, 43], branch="foreman/b-001")
    state = ledger.fold(ledger.read_events(root))
    batch = state.batches["b-001"]
    assert batch["issues"] == [42, 43]
    assert batch["state"] == "planned"
    assert batch["ci_gate"] == "pending"
    assert batch["review_gate"] == "pending"


def test_retriage_overwrites_earlier_verdict(root):
    ledger.append(root, "issue.triaged", issue=42, verdict="needs-info")
    ledger.append(root, "issue.triaged", issue=42, verdict="actionable")
    state = ledger.fold(ledger.read_events(root))
    assert state.issues[42]["verdict"] == "actionable"
    assert len(state.issues) == 1


def test_fold_collects_escalations_and_flakes(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    ledger.append(root, "escalation", batch="b-001", reason="push cap reached")
    ledger.append(root, "flake.observed", job="integration", test="test_login")
    ledger.append(root, "flake.observed", job="integration", test="test_login")
    state = ledger.fold(ledger.read_events(root))
    assert len(state.escalations) == 1
    assert state.flakes["integration::test_login"] == 2


# --- state machine ------------------------------------------------------------

def test_legal_transition_is_recorded(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    ledger.transition(root, "b-001", "building")
    state = ledger.fold(ledger.read_events(root))
    assert state.batches["b-001"]["state"] == "building"


def test_illegal_transition_is_refused(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    with pytest.raises(ledger.IllegalTransition) as exc:
        ledger.transition(root, "b-001", "merged")
    assert "planned" in str(exc.value) and "merged" in str(exc.value)


def test_refused_transition_writes_no_event(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    before = len(ledger.read_events(root))
    with pytest.raises(ledger.IllegalTransition):
        ledger.transition(root, "b-001", "merged")
    assert len(ledger.read_events(root)) == before


def test_terminal_state_accepts_no_further_transition(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    for s in ("building", "built", "open"):
        ledger.transition(root, "b-001", s)
    ledger.gate(root, "b-001", "ci", "full_green")
    ledger.gate(root, "b-001", "review", "clean")
    ledger.transition(root, "b-001", "ready")
    ledger.transition(root, "b-001", "merging")
    ledger.transition(root, "b-001", "merged")
    with pytest.raises(ledger.IllegalTransition):
        ledger.transition(root, "b-001", "open")


# --- gates --------------------------------------------------------------------

def _open_batch(root, bid="b-001"):
    ledger.append(root, "batch.created", batch=bid, issues=[1])
    for s in ("building", "built", "open"):
        ledger.transition(root, bid, s)


def test_ready_refused_while_a_gate_is_pending(root):
    _open_batch(root)
    ledger.gate(root, "b-001", "ci", "full_green")
    with pytest.raises(ledger.GateNotClear) as exc:
        ledger.transition(root, "b-001", "ready")
    assert "review" in str(exc.value)


def test_ready_refused_when_only_cheap_ci_is_green(root):
    _open_batch(root)
    ledger.gate(root, "b-001", "ci", "cheap_green")
    ledger.gate(root, "b-001", "review", "clean")
    with pytest.raises(ledger.GateNotClear):
        ledger.transition(root, "b-001", "ready")


def test_ready_allowed_when_both_gates_clear(root):
    _open_batch(root)
    ledger.gate(root, "b-001", "ci", "full_green")
    ledger.gate(root, "b-001", "review", "clean")
    ledger.transition(root, "b-001", "ready")
    assert ledger.fold(ledger.read_events(root)).batches["b-001"]["state"] == "ready"


def test_expensive_tier_gated_on_cheap_ci_and_review(root):
    _open_batch(root)
    state = ledger.fold(ledger.read_events(root))
    assert not ledger.may_run_expensive_tier(state.batches["b-001"])
    ledger.gate(root, "b-001", "ci", "cheap_green")
    state = ledger.fold(ledger.read_events(root))
    assert not ledger.may_run_expensive_tier(state.batches["b-001"])
    ledger.gate(root, "b-001", "review", "clean")
    state = ledger.fold(ledger.read_events(root))
    assert ledger.may_run_expensive_tier(state.batches["b-001"])


def test_failed_gate_reverts_ready_batch_to_blocked(root):
    _open_batch(root)
    ledger.gate(root, "b-001", "ci", "full_green")
    ledger.gate(root, "b-001", "review", "clean")
    ledger.transition(root, "b-001", "ready")
    ledger.gate(root, "b-001", "ci", "failed")
    state = ledger.fold(ledger.read_events(root))
    assert state.batches["b-001"]["state"] == "blocked"


def test_new_push_resets_gates_to_pending(root):
    _open_batch(root)
    ledger.gate(root, "b-001", "ci", "failed")
    ledger.append(root, "batch.pushed", batch="b-001", sha="abc123")
    batch = ledger.fold(ledger.read_events(root)).batches["b-001"]
    assert batch["ci_gate"] == "pending"
    assert batch["review_gate"] == "pending"
    assert batch["attempts"]["pushes"] == 1


# --- attempt counters (these drive escalation) --------------------------------

def test_counters_track_pushes_reviews_and_reruns(root):
    _open_batch(root)
    ledger.append(root, "batch.pushed", batch="b-001", sha="a")
    ledger.append(root, "batch.pushed", batch="b-001", sha="b")
    ledger.gate(root, "b-001", "review", "changes_requested")
    ledger.append(root, "ci.rerun", batch="b-001", job="integration")
    batch = ledger.fold(ledger.read_events(root)).batches["b-001"]
    assert batch["attempts"] == {"pushes": 2, "review_rounds": 1, "reruns": 1}


@pytest.mark.parametrize(
    "counters,caps,expected",
    [
        ({"pushes": 2, "review_rounds": 0, "reruns": 0}, {"pushes": 3}, None),
        ({"pushes": 3, "review_rounds": 0, "reruns": 0}, {"pushes": 3}, "pushes"),
        ({"pushes": 0, "review_rounds": 2, "reruns": 0}, {"review_rounds": 2}, "review_rounds"),
        ({"pushes": 0, "review_rounds": 0, "reruns": 3}, {"reruns": 2}, "reruns"),
    ],
)
def test_cap_breach_names_the_counter_that_broke(counters, caps, expected):
    assert ledger.cap_breached({"attempts": counters}, caps) == expected
