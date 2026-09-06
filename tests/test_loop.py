"""The scheduler: given the ledger, what is the single best next action?"""

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ledger  # noqa: E402
import loop  # noqa: E402

_JUST_NOW = datetime.now(UTC).isoformat()

CONFIG = {
    "auto_merge": True,
    "caps": {"pushes": 3, "review_rounds": 5, "reruns": 2},
    "limits": {"max_open_prs": 2, "max_ci_minutes_per_day": 60},
    "protected_paths": [],
}


def state_with(*batches, issues=None, spend=(), last_triage_at=_JUST_NOW):
    """A world in which triage has just run, unless a test says otherwise.

    Most tests are about what the loop does with work it already knows about,
    so they need triage to be up to date; the tests that care about picking up
    new issues pass last_triage_at explicitly.
    """
    st = ledger.State()
    for b in batches:
        base = {
            "id": b["id"],
            "issues": b.get("issues", [1]),
            "state": b["state"],
            "ci_gate": b.get("ci_gate", "pending"),
            "review_gate": b.get("review_gate", "pending"),
            "attempts": b.get("attempts", {"pushes": 0, "review_rounds": 0, "reruns": 0}),
            "paths": b.get("paths", ["src/x.py"]),
            "pr": b.get("pr"),
        }
        st.batches[b["id"]] = base
    st.issues = issues or {}
    st.ci_spend = list(spend)
    st.last_triage_at = last_triage_at
    return st


# --- priority: finish what is started before starting more --------------------


def test_a_ready_batch_is_merged_first():
    st = state_with(
        {"id": "b-001", "state": "planned"},
        {"id": "b-002", "state": "ready", "ci_gate": "full_green", "review_gate": "clean"},
    )
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "merge" and action["batch"] == "b-002"


def test_a_blocked_batch_outranks_starting_a_new_one():
    st = state_with({"id": "b-001", "state": "planned"}, {"id": "b-002", "state": "blocked"})
    assert loop.next_action(st, CONFIG)["do"] == "unblock"


def test_an_open_batch_awaiting_gates_is_watched():
    st = state_with({"id": "b-001", "state": "open", "ci_gate": "pending", "pr": 4})
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "watch" and action["batch"] == "b-001"


def test_a_built_batch_opens_its_pr():
    st = state_with({"id": "b-001", "state": "built"})
    assert loop.next_action(st, CONFIG)["do"] == "open_pr"


def test_a_planned_batch_is_built_when_nothing_older_needs_attention():
    st = state_with({"id": "b-001", "state": "planned"})
    assert loop.next_action(st, CONFIG)["do"] == "build"


# --- work in progress limits --------------------------------------------------


def test_no_new_build_while_the_open_pr_limit_is_reached():
    st = state_with(
        {"id": "b-001", "state": "open", "pr": 1, "ci_gate": "full_green", "review_gate": "clean"},
        {"id": "b-002", "state": "open", "pr": 2, "ci_gate": "full_green", "review_gate": "clean"},
        {"id": "b-003", "state": "planned"},
    )
    # both open batches have clear gates, so they advance rather than idle
    action = loop.next_action(st, CONFIG)
    assert action["do"] != "build"


def test_the_open_pr_limit_counts_only_live_prs():
    st = state_with(
        {"id": "b-001", "state": "merged", "pr": 1},
        {"id": "b-002", "state": "abandoned", "pr": 2},
        {"id": "b-003", "state": "planned"},
    )
    assert loop.in_flight_count(st) == 0
    assert loop.next_action(st, CONFIG)["do"] == "build"


# --- things the loop must not pick up ----------------------------------------


def test_escalated_batches_are_left_alone():
    st = state_with({"id": "b-001", "state": "escalated"})
    assert loop.next_action(st, CONFIG)["do"] == "idle"


def test_a_batch_at_its_cap_is_not_retried():
    st = state_with(
        {
            "id": "b-001",
            "state": "blocked",
            "attempts": {"pushes": 3, "review_rounds": 0, "reruns": 0},
        }
    )
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "escalate" and "pushes" in action["reason"]


def test_a_ready_batch_whose_paths_are_unconfirmed_is_merged_via_the_recipe_not_escalated():
    """Issue #76. `merge_blockers` refuses a list nobody checked against the
    diff — but the recipe `merge` dispatches is what checks it, so for the loop
    an unconfirmed list is the next step, not a person's problem."""
    st = state_with(
        {"id": "b-002", "state": "ready", "ci_gate": "full_green", "review_gate": "clean"}
    )
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "merge"
    assert "confirm" in action["reason"]


def test_a_ready_batch_whose_paths_are_confirmed_merges_with_nothing_to_add():
    st = state_with(
        {"id": "b-002", "state": "ready", "ci_gate": "full_green", "review_gate": "clean"}
    )
    st.batches["b-002"]["paths_head"] = "c" * 40
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "merge"
    assert "nothing blocks" in action["reason"]


def test_a_ready_batch_that_never_gets_merged_escalates_on_the_staleness_window():
    """`ready` is a wait on the recipe requesting the merge. One it keeps failing
    to request — `blockers` refusing it at step 5 every time — drew `merge` on
    every tick with nothing counting, the shape #77 closed for `merging`."""
    st = state_with(
        {"id": "b-002", "state": "ready", "ci_gate": "full_green", "review_gate": "clean"}
    )
    st.batches["b-002"]["updated"] = _aged(5)
    cfg = {**CONFIG, "limits": {**CONFIG["limits"], "stale_after_s": 3600}}
    action = loop.next_action(st, cfg)
    assert action["do"] == "escalate" and "stale" in action["reason"]


def test_a_ready_batch_with_a_blocker_escalates_rather_than_merging():
    st = state_with(
        {
            "id": "b-001",
            "state": "ready",
            "ci_gate": "full_green",
            "review_gate": "clean",
            "paths": ["src/auth/x.py"],
        }
    )
    action = loop.next_action(st, {**CONFIG, "protected_paths": ["**/auth/**"]})
    assert action["do"] == "escalate" and "auth" in action["reason"]


# --- new work -----------------------------------------------------------------


def test_untriaged_issues_are_triaged_when_nothing_is_in_flight():
    st = state_with(issues={}, last_triage_at=None)
    assert loop.next_action(st, CONFIG)["do"] == "triage"


def test_triaged_but_ungrouped_issues_are_batched():
    st = state_with(issues={1: {"issue": 1, "verdict": "actionable"}})
    assert loop.next_action(st, CONFIG)["do"] == "batch"


def test_issues_already_in_a_batch_are_not_batched_again():
    st = state_with(
        {
            "id": "b-001",
            "state": "open",
            "issues": [1],
            "pr": 3,
            "ci_gate": "full_green",
            "review_gate": "clean",
        },
        issues={1: {"issue": 1, "verdict": "actionable"}},
    )
    assert loop.next_action(st, CONFIG)["do"] != "batch"


def test_nothing_to_do_is_idle_not_an_error():
    st = state_with(
        {"id": "b-001", "state": "merged"}, issues={1: {"issue": 1, "verdict": "needs-repro"}}
    )
    assert loop.next_action(st, CONFIG)["do"] == "idle"


# --- the budget ---------------------------------------------------------------


def _spend(seconds, days_ago=0):
    ts = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
    return {"ts": ts, "seconds": seconds}


def test_todays_spend_is_summed_and_older_spend_ignored():
    st = state_with(spend=[_spend(600), _spend(300), _spend(9999, days_ago=2)])
    assert loop.spent_today(st) == 900


def test_budget_remaining_counts_down_from_the_configured_ceiling():
    st = state_with(spend=[_spend(600)])
    assert loop.budget_remaining(st, CONFIG) == 60 * 60 - 600


def test_an_exhausted_budget_stops_work_that_would_spend_ci():
    st = state_with({"id": "b-001", "state": "planned"}, spend=[_spend(60 * 60)])
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "idle" and "budget" in action["reason"].lower()


def test_an_exhausted_budget_still_allows_triage_which_costs_no_ci():
    st = state_with(issues={}, spend=[_spend(60 * 60)], last_triage_at=None)
    assert loop.next_action(st, CONFIG)["do"] == "triage"


def test_no_configured_budget_means_no_budget_limit():
    st = state_with({"id": "b-001", "state": "planned"}, spend=[_spend(99999)])
    cfg = {**CONFIG, "limits": {"max_open_prs": 2}}
    assert loop.budget_remaining(st, cfg) is None
    assert loop.next_action(st, cfg)["do"] == "build"


# --- review convergence, not rounds elapsed ----------------------------------


def _with_reviews(st, batch_id, *rounds):
    st.reviews = [
        {"batch": batch_id, "verdict": "changes_requested", "findings": list(f)} for f in rounds
    ]
    return st


def test_repeating_findings_escalate():
    st = state_with(
        {
            "id": "b-001",
            "state": "open",
            "pr": 1,
            "ci_gate": "full_green",
            "review_gate": "changes_requested",
        }
    )
    f = [{"file": "src/a.py", "severity": "high", "summary": "auth vocabulary is incomplete"}]
    _with_reviews(st, "b-001", f, f)
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "escalate" and "repeat" in action["reason"].lower()


def test_different_findings_each_round_keep_the_batch_moving():
    st = state_with(
        {
            "id": "b-001",
            "state": "open",
            "pr": 1,
            "ci_gate": "full_green",
            "review_gate": "changes_requested",
        }
    )
    _with_reviews(
        st,
        "b-001",
        [{"file": "src/a.py", "severity": "high", "summary": "retry loop unbounded"}],
        [{"file": "src/b.py", "severity": "high", "summary": "plurals lost from vocabulary"}],
    )
    action = loop.next_action(st, CONFIG)
    assert action["do"] != "escalate", "progress is not deadlock"


def test_a_batch_whose_review_requested_changes_is_worked_not_watched():
    """A gate that has already answered is not something to wait on."""
    st = state_with(
        {
            "id": "b-001",
            "state": "open",
            "pr": 1,
            "ci_gate": "full_green",
            "review_gate": "changes_requested",
        }
    )
    _with_reviews(
        st, "b-001", [{"file": "src/a.py", "severity": "high", "summary": "retry loop unbounded"}]
    )
    assert loop.next_action(st, CONFIG)["do"] == "unblock"


def test_a_batch_still_awaiting_its_review_is_watched():
    st = state_with(
        {"id": "b-001", "state": "open", "pr": 1, "ci_gate": "full_green", "review_gate": "pending"}
    )
    assert loop.next_action(st, CONFIG)["do"] == "watch"


# --- issue #19: no gate may pin a batch on watch forever ---------------------


def _aged(hours):
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def test_a_batch_watched_past_the_staleness_window_escalates():
    st = state_with({"id": "b-001", "state": "open", "pr": 1, "ci_gate": "pending"})
    st.batches["b-001"]["updated"] = _aged(5)
    cfg = {**CONFIG, "limits": {**CONFIG["limits"], "stale_after_s": 3600}}
    action = loop.next_action(st, cfg)
    assert action["do"] == "escalate" and "stale" in action["reason"].lower()


def test_a_batch_still_inside_the_window_is_watched():
    st = state_with({"id": "b-001", "state": "open", "pr": 1, "ci_gate": "pending"})
    st.batches["b-001"]["updated"] = _aged(0)
    cfg = {**CONFIG, "limits": {**CONFIG["limits"], "stale_after_s": 3600}}
    assert loop.next_action(st, cfg)["do"] == "watch"


def test_no_staleness_window_configured_means_no_staleness_escalation():
    st = state_with({"id": "b-001", "state": "open", "pr": 1, "ci_gate": "pending"})
    st.batches["b-001"]["updated"] = _aged(500)
    cfg = {**CONFIG, "limits": {k: v for k, v in CONFIG["limits"].items()}}
    assert loop.next_action(st, cfg)["do"] == "watch"


def test_a_batch_with_no_timestamp_is_never_called_stale():
    st = state_with({"id": "b-001", "state": "open", "pr": 1, "ci_gate": "pending"})
    st.batches["b-001"]["updated"] = None
    cfg = {**CONFIG, "limits": {**CONFIG["limits"], "stale_after_s": 3600}}
    assert loop.next_action(st, cfg)["do"] == "watch"


# --- a batch in the merge queue is live, and had no action at all -------------


def _merging(**over):
    """A batch driven planned -> merging, both gates clear, waiting on GitHub."""
    return {
        "id": "b-001",
        "state": "merging",
        "pr": 1,
        "ci_gate": "full_green",
        "review_gate": "clean",
        **over,
    }


def test_a_batch_in_the_merge_queue_is_watched():
    """`merging` is in IN_FLIGHT but had no branch in next_action, so it got no
    action, no staleness check and no governor — while still holding a slot
    against max_open_prs."""
    st = state_with(_merging())
    st.batches["b-001"]["progress_at"] = _aged(0)
    cfg = {**CONFIG, "limits": {**CONFIG["limits"], "stale_after_s": 3600}}
    action = loop.next_action(st, cfg)
    assert action["do"] == "watch" and action["batch"] == "b-001"


def test_a_merge_that_never_completes_escalates():
    """`gh pr merge --auto` is fire-and-forget: a queue that refuses it, or never
    fires, leaves the batch here for good and nothing brings it back."""
    st = state_with(_merging())
    st.batches["b-001"]["progress_at"] = _aged(68)
    cfg = {**CONFIG, "limits": {**CONFIG["limits"], "stale_after_s": 3600}}
    action = loop.next_action(st, cfg)
    assert action["do"] == "escalate" and action["batch"] == "b-001"
    assert "stale" in action["reason"].lower()


def test_a_merging_batch_with_no_staleness_window_is_still_only_watched():
    """Same rule as every other wait: no window configured, no escalation."""
    st = state_with(_merging())
    st.batches["b-001"]["progress_at"] = _aged(500)
    assert loop.next_action(st, CONFIG)["do"] == "watch"


def test_a_merging_batch_is_drained_before_one_that_is_only_ready():
    """Nearest the merge first — the merge for this one is already requested."""
    st = state_with(
        _merging(),
        {"id": "b-002", "state": "ready", "ci_gate": "full_green", "review_gate": "clean"},
    )
    st.batches["b-001"]["progress_at"] = _aged(0)
    assert loop.next_action(st, CONFIG)["batch"] == "b-001"


# --- issue #53: triage must stay reachable after the first batch --------------


def test_triage_is_offered_again_once_the_ledger_has_batches():
    """The only path to triage used to require an empty ledger, so after the
    first batch was ever created the loop could never pick up new issues."""
    st = state_with({"id": "b-001", "state": "merged", "pr": 1}, last_triage_at=None)
    assert loop.next_action(st, CONFIG)["do"] == "triage"


def test_a_recent_triage_is_not_immediately_repeated():
    st = state_with({"id": "b-001", "state": "merged", "pr": 1})
    assert loop.next_action(st, CONFIG)["do"] == "idle"


def test_a_stale_triage_is_offered_again():
    stale = (datetime.now(UTC) - timedelta(hours=9)).isoformat()
    st = state_with({"id": "b-001", "state": "merged", "pr": 1}, last_triage_at=stale)
    assert loop.next_action(st, CONFIG)["do"] == "triage"


def test_a_due_triage_outranks_an_exhausted_budget_because_it_costs_no_ci():
    st = state_with(
        {"id": "b-001", "state": "planned"}, spend=[_spend(60 * 60)], last_triage_at=None
    )
    assert loop.next_action(st, CONFIG)["do"] == "triage"


def test_triage_can_be_switched_off_entirely():
    st = state_with({"id": "b-001", "state": "merged", "pr": 1}, last_triage_at=None)
    config = {**CONFIG, "limits": {**CONFIG["limits"], "triage_every_s": 0}}
    assert loop.next_action(st, config)["do"] == "idle"


def test_triage_is_switched_off_on_an_empty_ledger_too():
    """The empty-ledger branch used to sit ahead of the refresh gate, so
    triage_every_s: 0 was ignored on exactly the repo that has never run."""
    st = state_with(last_triage_at=None)
    config = {**CONFIG, "limits": {**CONFIG["limits"], "triage_every_s": 0}}
    assert loop.next_action(st, config)["do"] == "idle"


def test_an_empty_ledger_still_says_so():
    st = state_with(last_triage_at=None)
    assert loop.next_action(st, CONFIG)["reason"] == "nothing in the ledger yet"


# --- issue #17: the push cap counts volume; convergence is what matters -------

# What config.example.json ships: a loose runaway ceiling on raw pushes, with
# the real judgement made by the convergence check below.
LOOSE_CAPS = {**CONFIG, "caps": {"pushes": 8, "review_rounds": 5, "reruns": 2}}


def _attempts(**counts):
    return {"pushes": 0, "review_rounds": 0, "reruns": 0, "futile_pushes": 0, **counts}


def test_pushes_that_keep_leaving_ci_red_the_same_way_escalate():
    st = state_with(
        {
            "id": "b-001",
            "state": "blocked",
            "ci_gate": "failed",
            "attempts": _attempts(pushes=3, futile_pushes=3),
        }
    )
    action = loop.next_action(st, LOOSE_CAPS)
    assert action["do"] == "escalate" and "diagnosis" in action["reason"]


def test_three_pushes_that_each_resolved_review_findings_do_not_escalate():
    """PR #7: three pushes, three rounds of findings, each one cleared.

    A PR that survives three genuine review rounds is a good PR; counting the
    pushes it took punishes it for being reviewed properly.
    """
    st = state_with(
        {
            "id": "b-001",
            "state": "blocked",
            "ci_gate": "failed",
            "attempts": _attempts(pushes=3, review_rounds=3),
        }
    )
    assert loop.next_action(st, LOOSE_CAPS)["do"] == "unblock"


def test_the_raw_push_count_is_still_a_hard_runaway_ceiling():
    st = state_with(
        {"id": "b-001", "state": "blocked", "attempts": _attempts(pushes=8, futile_pushes=0)}
    )
    action = loop.next_action(st, LOOSE_CAPS)
    assert action["do"] == "escalate" and "pushes at cap" in action["reason"]


def test_the_futile_push_ceiling_can_be_configured():
    st = state_with(
        {
            "id": "b-001",
            "state": "blocked",
            "ci_gate": "failed",
            "attempts": _attempts(pushes=2, futile_pushes=2),
        }
    )
    assert loop.next_action(st, LOOSE_CAPS)["do"] == "unblock"
    tight = {**LOOSE_CAPS, "caps": {**LOOSE_CAPS["caps"], "futile_pushes": 2}}
    assert loop.next_action(st, tight)["do"] == "escalate"


# --- issue #58: a merged batch must not hide an issue it left open -----------


def _triaged(number, at):
    return {number: {"issue": number, "verdict": "actionable", "ts": at}}


def test_a_plan_built_before_the_merge_and_applied_after_it_is_not_a_sighting_after_it():
    """Issue #80. The record was written an hour ago, after b-001 merged two
    hours ago — but the plan it came from looked at the tracker five hours ago,
    when #5 was legitimately open. Dating the sighting by the apply made the
    merge look as though it had failed to close the issue."""
    st = state_with({"id": "b-001", "state": "merged", "issues": [5], "pr": 7})
    st.batches["b-001"]["progress_at"] = _aged(2)
    st.issues = {5: {"issue": 5, "verdict": "actionable", "ts": _aged(1), "observed_at": _aged(5)}}
    st.open_seen_at = {5: _aged(5)}
    assert loop.merged_leaving_open(st) == []
    assert loop.next_action(st, CONFIG)["do"] != "escalate"


def test_a_plan_that_looked_after_the_merge_is_still_a_sighting_after_it():
    st = state_with({"id": "b-001", "state": "merged", "issues": [5], "pr": 7})
    st.batches["b-001"]["progress_at"] = _aged(2)
    st.issues = {5: {"issue": 5, "verdict": "actionable", "ts": _aged(0), "observed_at": _aged(1)}}
    assert loop.merged_leaving_open(st) == [{"batch": "b-001", "issues": [5]}]


def test_an_issue_its_merged_batch_left_open_reaches_a_human():
    """b-001 merged as PR #7, but issue #5 is still open on the tracker.

    Triage lists open issues only, so a sighting after the merge is evidence
    the merge did not close it. What that evidence means, though, is not
    something the ledger can settle: either the PR merged without a closing
    keyword and the fix is on trunk, or the fix did not fix it. Those want
    opposite actions, so the loop hands the choice over instead of guessing.
    """
    st = state_with({"id": "b-001", "state": "merged", "issues": [5], "pr": 7})
    st.batches["b-001"]["progress_at"] = _aged(48)
    st.issues = _triaged(5, _aged(1))
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "escalate"
    assert action["issues"] == [5] and action["merged_batch"] == "b-001"


def test_an_issue_its_merged_batch_left_open_is_never_offered_for_batching():
    """The action a release produced was one nothing could take.

    `batch.py plan` groups `triage_out["triaged"]`, and the issues this rule
    finds are the ones triage *skips* — `should_skip` refuses to re-triage an
    issue whose `updatedAt` has not moved, and merging without a closing
    keyword does not move it. So `do: batch` came back every tick forever, no
    counter moved, and because that branch sits above `triage_due` the loop
    stopped looking for new issues at all.
    """
    st = state_with({"id": "b-001", "state": "merged", "issues": [5], "pr": 7})
    st.batches["b-001"]["progress_at"] = _aged(48)
    st.issues = _triaged(5, _aged(1))
    assert 5 in loop._grouped_issues(st)
    assert loop.next_action(st, CONFIG)["do"] != "batch"


def test_the_loop_says_it_once_and_then_moves_on():
    """The bound. A recorded escalation is the end of the loop's involvement.

    Nothing here can be retried into working, so repeating it is pure noise —
    and an unbounded escalation costs a session every fifteen minutes just as
    an unbounded `batch` did.
    """
    st = state_with({"id": "b-001", "state": "merged", "issues": [5], "pr": 7})
    st.batches["b-001"]["progress_at"] = _aged(48)
    st.issues = _triaged(5, _aged(1))
    st.escalations = [{"type": "escalation", "issues": [5], "reason": "b-001 merged; #5 open"}]
    assert loop.next_action(st, CONFIG)["do"] == "idle"


def test_an_issue_another_batch_is_still_working_on_does_not_escalate():
    """b-002 merged without closing #5, but b-001 has an open PR for it.

    A live batch will close it. Telling a human about an issue somebody is
    already fixing is the noise that makes NEEDS YOU unreadable.
    """
    st = state_with(
        {"id": "b-001", "state": "open", "issues": [5], "pr": 7, "ci_gate": "pending"},
        {"id": "b-002", "state": "merged", "issues": [5], "pr": 6},
    )
    st.batches["b-002"]["progress_at"] = _aged(48)
    st.issues = _triaged(5, _aged(1))
    assert loop.merged_leaving_open(st) == []


def test_a_merged_batch_that_left_two_issues_open_escalates_once_naming_both():
    st = state_with({"id": "b-001", "state": "merged", "issues": [5, 9], "pr": 7})
    st.batches["b-001"]["progress_at"] = _aged(48)
    st.issues = {**_triaged(5, _aged(1)), **_triaged(9, _aged(1))}
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "escalate" and action["issues"] == [5, 9]


def test_the_escalation_a_merged_batch_produces_is_shown_in_needs_you():
    """The whole point of choosing to escalate: somebody reads it in the morning.

    `status._needs_human` drops an escalation whose batch is no longer
    `escalated`, on the premise that a merged batch needs nobody. This is the
    case that disproves it, so the record is keyed on the issues that need a
    person rather than on the batch that is already finished.
    """
    import status

    st = state_with({"id": "b-001", "state": "merged", "issues": [5], "pr": 7})
    st.batches["b-001"]["progress_at"] = _aged(48)
    st.issues = _triaged(5, _aged(1))
    action = loop.next_action(st, CONFIG)
    st.escalations = [
        {"type": "escalation", "issues": action["issues"], "reason": action["reason"]}
    ]
    lines = status._needs_human(st, CONFIG["caps"])
    assert len(lines) == 1 and "#5" in lines[0]


def test_an_issue_whose_merged_batch_closed_it_stays_out_of_the_queue():
    """No triage has seen it since the merge, so nothing says it is still open."""
    st = state_with({"id": "b-001", "state": "merged", "issues": [5], "pr": 7})
    st.batches["b-001"]["progress_at"] = _aged(1)
    st.issues = _triaged(5, _aged(48))
    assert loop.next_action(st, CONFIG)["do"] != "batch"


def test_an_issue_held_by_an_escalated_batch_is_not_re_batched():
    """A human is deciding what happens to it; a second batch would collide."""
    st = state_with({"id": "b-001", "state": "escalated", "issues": [5]})
    st.batches["b-001"]["progress_at"] = _aged(48)
    st.issues = _triaged(5, _aged(1))
    assert loop.next_action(st, CONFIG)["do"] != "batch"


def test_an_issue_a_live_batch_is_working_on_is_never_re_batched():
    st = state_with(
        {"id": "b-001", "state": "open", "issues": [5], "pr": 7, "ci_gate": "pending"},
        {"id": "b-002", "state": "merged", "issues": [5], "pr": 6},
    )
    st.batches["b-002"]["progress_at"] = _aged(48)
    st.issues = _triaged(5, _aged(1))
    assert loop.next_action(st, CONFIG)["do"] == "watch"


def _merged_batch_events(issue, triaged_at, merged_at):
    """#5 triaged, batched, and merged without a closing keyword."""
    return [
        {
            "ts": triaged_at,
            "type": "issue.triaged",
            "issue": issue,
            "verdict": "actionable",
            "issue_updated_at": "2026-01-02T00:00:00Z",
        },
        {"ts": triaged_at, "type": "batch.created", "batch": "b-001", "issues": [issue]},
        *(
            {"ts": merged_at, "type": "batch.state", "batch": "b-001", "from": prev, "to": nxt}
            for prev, nxt in (
                ("planned", "building"),
                ("building", "built"),
                ("built", "open"),
                ("open", "ready"),
                ("ready", "merging"),
                ("merging", "merged"),
            )
        ),
    ]


def _triage_pass(at, saw):
    """A completed triage run and the open issues it listed."""
    return {"ts": at, "type": "triage.completed", "triaged": 0, "open_issues": list(saw)}


def test_a_merged_batch_escalates_an_issue_a_later_triage_saw_open():
    """The ordering the rule was written for, played end to end.

    #5 is triaged, a PR quotes it, the PR merges without a closing keyword, and
    every triage after that *skips* it — `triage.should_skip` refuses to
    re-triage an issue whose `updatedAt` has not moved, and merging without
    closing does not move it. So no `issue.triaged` newer than the merge is ever
    written; the triage pass itself is the evidence, because it asked GitHub for
    open issues and #5 came back.
    """
    events = _merged_batch_events(5, triaged_at=_aged(72), merged_at=_aged(48))
    events.append(_triage_pass(_aged(0.2), saw=[5]))
    action = loop.next_action(ledger.fold(events), CONFIG)
    assert action["do"] == "escalate" and action["issues"] == [5]
    assert "b-001" in action["reason"]


def test_the_escalation_ends_it_rather_than_repeating_every_tick():
    """Folded from the log, not set by hand: the suppression has a producer too."""
    events = _merged_batch_events(5, triaged_at=_aged(72), merged_at=_aged(48))
    events.append(_triage_pass(_aged(0.2), saw=[5]))
    action = loop.next_action(ledger.fold(events), CONFIG)
    events.append(
        {
            "ts": _aged(0.1),
            "type": "escalation",
            "issues": action["issues"],
            "merged_batch": action["merged_batch"],
            "reason": action["reason"],
        }
    )
    assert loop.next_action(ledger.fold(events), CONFIG)["do"] == "idle"


def test_a_triage_pass_that_did_not_see_the_issue_leaves_it_with_its_batch():
    """The merge closed it, so it is not in the open list any more."""
    events = _merged_batch_events(5, triaged_at=_aged(72), merged_at=_aged(48))
    events.append(_triage_pass(_aged(0.2), saw=[9]))
    assert loop.next_action(ledger.fold(events), CONFIG)["do"] == "idle"


def test_a_triage_pass_from_before_the_merge_is_not_evidence():
    """It saw the issue open, but that was before the PR that might have closed it."""
    events = _merged_batch_events(5, triaged_at=_aged(72), merged_at=_aged(48))
    events.insert(1, _triage_pass(_aged(70), saw=[5]))
    events.append(_triage_pass(_aged(0.2), saw=[]))
    assert loop.next_action(ledger.fold(events), CONFIG)["do"] == "idle"


# --- issue #62: a batch mid-build is in flight, not nowhere ------------------


def test_a_batch_left_mid_build_is_resumed_before_a_new_one_is_started():
    """A build interrupted by a crash or a closed laptop is exactly what the
    durable ledger exists to recover."""
    st = state_with({"id": "b-002", "state": "building"}, {"id": "b-003", "state": "planned"})
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "build" and action["batch"] == "b-002"


def test_a_build_in_progress_counts_against_the_open_work_limit():
    st = state_with({"id": "b-002", "state": "building"}, {"id": "b-003", "state": "planned"})
    assert loop.in_flight_count(st) == 1


def test_no_new_build_starts_while_every_slot_is_held_by_a_running_build():
    st = state_with(
        {"id": "b-001", "state": "building"},
        {"id": "b-002", "state": "building"},
        {"id": "b-003", "state": "planned"},
    )
    action = loop.next_action(st, CONFIG)
    assert loop.in_flight_count(st) == 2
    assert action["batch"] != "b-003", "the WIP limit is reached; drain before starting"


def test_a_mid_build_batch_is_not_resumed_once_the_ci_budget_is_spent():
    st = state_with({"id": "b-002", "state": "building"}, spend=[_spend(60 * 60)])
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "idle" and "budget" in action["reason"].lower()


# --- a resumable build must still be a bounded one ---------------------------


def _building_batch(root, resumes):
    """A batch parked in `building`, picked up again `resumes` times."""
    ledger.append(root, "batch.created", batch="b-9", issues=[1])
    for _ in range(resumes + 1):
        ledger.transition(root, "b-9", "building")


def test_a_build_resumed_past_the_ceiling_reaches_a_human(tmp_path):
    """Eleven resumes spread over months: no counter moved, `progress_at` never
    moved either, and `next_action` answered `build` forever."""
    root = ledger.init(tmp_path)
    _building_batch(root, resumes=10)
    st = ledger.load(root)
    st.last_triage_at = _JUST_NOW
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "escalate" and action["batch"] == "b-9"
    assert "resum" in action["reason"].lower()


def test_a_build_inside_the_ceiling_is_still_resumed(tmp_path):
    """Recovering an interrupted build is the point; only doing it forever is not."""
    root = ledger.init(tmp_path)
    _building_batch(root, resumes=1)
    st = ledger.load(root)
    st.last_triage_at = _JUST_NOW
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "build" and action["batch"] == "b-9"


def test_a_batch_that_finished_building_is_not_escalated_for_its_old_resumes(tmp_path):
    root = ledger.init(tmp_path)
    _building_batch(root, resumes=10)
    ledger.transition(root, "b-9", "built")
    st = ledger.load(root)
    st.last_triage_at = _JUST_NOW
    action = loop.next_action(st, CONFIG)
    assert action["do"] == "open_pr" and action["batch"] == "b-9"


def test_the_resume_ceiling_can_be_set_in_the_config(tmp_path):
    root = ledger.init(tmp_path)
    _building_batch(root, resumes=1)
    st = ledger.load(root)
    st.last_triage_at = _JUST_NOW
    cfg = {**CONFIG, "caps": {**CONFIG["caps"], "build_resumes": 1}}
    assert loop.next_action(st, cfg)["do"] == "escalate"


# --- issue #70: the caps must not vanish when the loop runs from a worktree ---


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=f@example.com", "-c", "user.name=foreman", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def worktree(tmp_path):
    """A checkout holding the real `.foreman/`, plus the build worktree holding none.

    `commands/build.md` prescribes exactly this layout, and the loop is run with
    its cwd inside the worktree, where `.foreman/config.json` does not exist.
    """
    checkout = tmp_path / "repo"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "root")
    linked = tmp_path / "foreman-b-001"
    _git(checkout, "worktree", "add", "-q", str(linked), "-b", "foreman/b-001")
    root = ledger.init(checkout)
    (root / ledger.CONFIG_FILE).write_text(json.dumps(CONFIG))
    return checkout, linked


def _runaway_batch(root):
    """A batch that has pushed 99 times: far past any cap anyone would configure."""
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    ledger.transition(root, "b-001", "building")
    ledger.transition(root, "b-001", "built")
    ledger.transition(root, "b-001", "open")
    for _ in range(99):
        ledger.append(root, "batch.pushed", batch="b-001", sha="deadbee")


def test_the_loop_reads_its_caps_from_the_repository_when_run_from_a_worktree(
    worktree, monkeypatch, capsys
):
    """With no config the push cap was absent, so `cap_breached` returned None and
    a batch at 99 pushes was never escalated — the loop just kept going."""
    checkout, linked = worktree
    _runaway_batch(checkout / ledger.LEDGER_DIR)
    monkeypatch.chdir(linked)

    assert loop.main(["next"]) == 0
    action = json.loads(capsys.readouterr().out)
    assert action["do"] == "escalate"
    assert "pushes at cap" in action["reason"]


def test_the_loop_says_so_when_it_is_running_with_no_config_at_all(worktree, monkeypatch, capsys):
    """A loop with no caps is the failure mode; it must not be a silent one."""
    checkout, linked = worktree
    (checkout / ledger.LEDGER_DIR / ledger.CONFIG_FILE).unlink()
    _runaway_batch(checkout / ledger.LEDGER_DIR)
    monkeypatch.chdir(linked)

    assert loop.main(["next"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["do"] != "escalate", "no config really does mean no caps"
    assert str(checkout / ledger.LEDGER_DIR / ledger.CONFIG_FILE) in captured.err


def test_an_explicit_config_path_is_still_obeyed(worktree, monkeypatch, tmp_path, capsys):
    checkout, linked = worktree
    elsewhere = tmp_path / "strict.json"
    elsewhere.write_text(json.dumps({**CONFIG, "caps": {"pushes": 1}}))
    _runaway_batch(checkout / ledger.LEDGER_DIR)
    monkeypatch.chdir(linked)

    assert loop.main(["next", "--config", str(elsewhere)]) == 0
    assert "(99/1)" in json.loads(capsys.readouterr().out)["reason"]
