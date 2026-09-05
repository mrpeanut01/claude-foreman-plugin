"""The scheduler: given the ledger, what is the single best next action?"""

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ledger  # noqa: E402
import loop  # noqa: E402

CONFIG = {
    "auto_merge": True,
    "caps": {"pushes": 3, "review_rounds": 2, "reruns": 2},
    "limits": {"max_open_prs": 2, "max_ci_minutes_per_day": 60},
    "protected_paths": [],
}


def state_with(*batches, issues=None, spend=()):
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
    st = state_with(issues={})
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
    st = state_with(issues={}, spend=[_spend(60 * 60)])
    assert loop.next_action(st, CONFIG)["do"] == "triage"


def test_no_configured_budget_means_no_budget_limit():
    st = state_with({"id": "b-001", "state": "planned"}, spend=[_spend(99999)])
    cfg = {**CONFIG, "limits": {"max_open_prs": 2}}
    assert loop.budget_remaining(st, cfg) is None
    assert loop.next_action(st, cfg)["do"] == "build"
