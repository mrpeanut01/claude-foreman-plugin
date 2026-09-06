"""Ledger: an append-only event log folded into current state."""

import json
import subprocess
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


# --- a line the fold cannot use is skipped, exactly like an unparseable one ---


def test_a_wrong_typed_field_does_not_take_the_fold_down(root):
    """`ledger.py append --type triage.completed --json '{"open_issues": 5}'`.

    A documented CLI, reachable by any agent, and the log is append-only — the
    line cannot be taken back out. `fold` is the single reader every script goes
    through, so this crashed `loop.py`, `status.py` and `land.py` from that line
    on, permanently.
    """
    ledger.append(root, "issue.triaged", issue=1, verdict="actionable")
    ledger.append(root, "triage.completed", open_issues=5)
    ledger.append(root, "issue.triaged", issue=2, verdict="actionable")
    state = ledger.fold(ledger.read_events(root))
    assert sorted(state.issues) == [1, 2]
    assert state.skipped_lines == 1


@pytest.mark.parametrize(
    "event",
    [
        {"type": "triage.completed", "open_issues": 5},
        {"type": "issue.triaged", "verdict": "actionable"},
        {"type": "batch.state", "batch": "b-001", "from": "planned"},
        {"type": "gate.set", "batch": "b-001", "value": "failed"},
        {"type": "gate.set", "batch": "b-001", "gate": "ci"},
    ],
)
def test_an_event_missing_or_mistyping_a_field_it_needs_is_counted_and_skipped(event):
    """`open_issues` is only the newest way in. A missing `to` or `gate` raises
    the same way through the same CLI, and guarding one field at a time leaves
    the next one to be found in production."""
    events = [
        {"ts": "2026-01-01T00:00:00Z", "type": "batch.created", "batch": "b-001", "issues": [1]},
        {"ts": "2026-01-02T00:00:00Z", **event},
        {"ts": "2026-01-03T00:00:00Z", "type": "batch.state", "batch": "b-001", "to": "building"},
    ]
    state = ledger.fold(events)
    assert state.skipped_lines == 1
    assert state.batches["b-001"]["state"] == "building"


def test_a_skipped_event_leaves_nothing_half_applied():
    """It must not count as movement either: `updated` is what staleness reads."""
    events = [
        {"ts": "2026-01-01T00:00:00Z", "type": "batch.created", "batch": "b-001", "issues": [1]},
        {"ts": "2026-01-09T00:00:00Z", "type": "gate.set", "batch": "b-001", "gate": "ci"},
    ]
    batch = ledger.fold(events).batches["b-001"]
    assert batch["updated"] == "2026-01-01T00:00:00Z"
    assert batch["ci_gate"] == "pending"


def test_a_healthy_ledger_skips_nothing(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    ledger.append(root, "triage.completed", triaged=1, open_issues=[1])
    assert ledger.fold(ledger.read_events(root)).skipped_lines == 0


def test_a_torn_line_is_counted_the_same_way_when_the_ledger_is_loaded(root):
    """From outside, a torn write and an unreadable event are one thing: the log
    holds something the state does not. Both have to reach the same counter, or
    the digest can only warn about half of them."""
    ledger.append(root, "issue.triaged", issue=1, verdict="actionable")
    (root / "events.jsonl").open("a").write("{ this is not json\n")
    ledger.append(root, "triage.completed", open_issues=5)
    state = ledger.load(root)
    assert sorted(state.issues) == [1]
    assert state.skipped_lines == 2


def test_a_triage_pass_that_could_not_be_recorded_does_not_move_the_clock():
    """`triage_due` reads `last_triage_at`. A pass whose sightings were thrown away
    must not count as the loop having looked, or the issues it failed to record
    go unlooked-for until the next window."""
    events = [
        {"ts": "2026-01-01T00:00:00Z", "type": "triage.completed", "open_issues": [1]},
        {"ts": "2026-01-02T00:00:00Z", "type": "triage.completed", "open_issues": 5},
    ]
    state = ledger.fold(events)
    assert state.skipped_lines == 1
    assert state.last_triage_at == "2026-01-01T00:00:00Z"


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
    assert batch["attempts"] == {
        "pushes": 2,
        "review_rounds": 1,
        "reruns": 1,
        "futile_pushes": 0,
        "build_resumes": 0,
    }


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


def test_ci_spend_is_recorded_for_the_budget(root):
    ledger.append(root, "ci.launched", batch="b-001", tier="cheap", seconds=120)
    ledger.append(root, "ci.launched", batch="b-001", tier="expensive", seconds=2280)
    state = ledger.fold(ledger.read_events(root))
    assert [e["seconds"] for e in state.ci_spend] == [120, 2280]


def test_a_batch_keeps_the_paths_and_risk_it_was_created_with(root):
    """merge_blockers reads batch['paths'] to enforce protected paths."""
    ledger.append(
        root, "batch.created", batch="b-001", issues=[3], paths=["src/auth/session.py"], risk="high"
    )
    batch = ledger.fold(ledger.read_events(root)).batches["b-001"]
    assert batch["paths"] == ["src/auth/session.py"]
    assert batch["risk"] == "high"


# --- issue #28: staleness must measure progress, not bookkeeping -------------


def test_re_recording_an_unchanged_gate_is_not_progress(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    for step in ("building", "built", "open"):
        ledger.transition(root, "b-001", step)
    before = ledger.fold(ledger.read_events(root)).batches["b-001"]["progress_at"]
    ledger.gate(root, "b-001", "ci", "pending")
    ledger.append(root, "ci.launched", batch="b-001", tier="cheap", seconds=10)
    after = ledger.fold(ledger.read_events(root)).batches["b-001"]
    assert after["progress_at"] == before, "polling must not reset the staleness clock"
    assert after["updated"] != before, "but the batch was still touched"


def test_a_gate_that_actually_changes_is_progress(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    ledger.transition(root, "b-001", "building")
    before = ledger.fold(ledger.read_events(root)).batches["b-001"]["progress_at"]
    ledger.gate(root, "b-001", "ci", "cheap_green")
    assert ledger.fold(ledger.read_events(root)).batches["b-001"]["progress_at"] != before


# --- issue #55: a duplicate batch.created must not destroy merged history -----


def test_recreating_an_existing_batch_id_does_not_erase_it():
    state = ledger.fold(
        [
            {"type": "batch.created", "batch": "b-001", "issues": [1, 2]},
            {"type": "batch.state", "batch": "b-001", "from": "planned", "to": "merged"},
            {"type": "batch.merged", "batch": "b-001", "pr": 7},
            {"type": "batch.created", "batch": "b-001", "issues": [3]},
        ]
    )
    assert state.batches["b-001"]["state"] == "merged"
    assert state.batches["b-001"]["issues"] == [1, 2]


# --- issue #58: which issues a triage pass found open ------------------------


def test_a_completed_triage_records_when_each_open_issue_was_last_seen(root):
    ledger.append(root, "triage.completed", triaged=1, open_issues=[5, 9])
    state = ledger.fold(ledger.read_events(root))
    assert set(state.open_seen_at) == {5, 9}
    assert state.open_seen_at[5] == state.last_triage_at


def test_a_triaged_issue_is_itself_a_sighting(root):
    """`fetch_issues` asks for open issues only, so a record is a sighting too."""
    ledger.append(root, "issue.triaged", issue=5, verdict="actionable")
    state = ledger.fold(ledger.read_events(root))
    assert state.open_seen_at[5] == state.issues[5]["ts"]


def test_a_later_pass_that_did_not_list_an_issue_leaves_its_last_sighting_alone(root):
    ledger.append(root, "triage.completed", triaged=0, open_issues=[5])
    first = ledger.fold(ledger.read_events(root)).open_seen_at[5]
    ledger.append(root, "triage.completed", triaged=0, open_issues=[])
    assert ledger.fold(ledger.read_events(root)).open_seen_at[5] == first


def test_an_older_ledger_with_no_open_issues_field_still_folds(root):
    ledger.append(root, "triage.completed", triaged=0, failed=0)
    state = ledger.fold(ledger.read_events(root))
    assert state.open_seen_at == {} and state.last_triage_at


# --- issue #17: the push cap must measure diagnosis, not volume ---------------


def test_a_push_that_leaves_ci_failing_the_same_way_is_counted_as_futile(root):
    _open_batch(root)
    ledger.gate(root, "b-001", "ci", "failed")
    ledger.append(root, "batch.pushed", batch="b-001", sha="a")
    ledger.gate(root, "b-001", "ci", "failed")
    attempts = ledger.fold(ledger.read_events(root)).batches["b-001"]["attempts"]
    assert attempts["futile_pushes"] == 1
    assert attempts["pushes"] == 1, "the raw count is still the runaway ceiling"


def test_futile_pushes_accumulate_while_ci_keeps_coming_back_red(root):
    _open_batch(root)
    for _ in range(3):
        ledger.gate(root, "b-001", "ci", "failed")
        ledger.append(root, "batch.pushed", batch="b-001", sha="a")
    ledger.gate(root, "b-001", "ci", "failed")
    assert ledger.fold(ledger.read_events(root)).batches["b-001"]["attempts"]["futile_pushes"] == 3


def test_a_push_that_turns_ci_green_ends_the_futile_run(root):
    _open_batch(root)
    ledger.gate(root, "b-001", "ci", "failed")
    ledger.append(root, "batch.pushed", batch="b-001", sha="a")
    ledger.gate(root, "b-001", "ci", "failed")
    ledger.append(root, "batch.pushed", batch="b-001", sha="b")
    ledger.gate(root, "b-001", "ci", "cheap_green")
    assert ledger.fold(ledger.read_events(root)).batches["b-001"]["attempts"]["futile_pushes"] == 0


def test_pushes_that_answer_review_findings_are_not_futile(root):
    """PR #7: three pushes, each clearing the previous round's findings.

    CI was green throughout; only the reviewer kept finding new things. That is
    a PR being reviewed properly, and the push cap must not punish it.
    """
    _open_batch(root)
    for _ in range(3):
        ledger.gate(root, "b-001", "review", "changes_requested")
        ledger.append(root, "batch.pushed", batch="b-001", sha="a")
        ledger.gate(root, "b-001", "ci", "full_green")
    attempts = ledger.fold(ledger.read_events(root)).batches["b-001"]["attempts"]
    assert attempts["pushes"] == 3
    assert attempts["futile_pushes"] == 0


def test_ci_failing_twice_without_a_push_between_is_not_a_futile_push(root):
    """Nothing was attempted, so nothing failed to fix it."""
    _open_batch(root)
    ledger.gate(root, "b-001", "ci", "failed")
    ledger.gate(root, "b-001", "ci", "failed")
    assert ledger.fold(ledger.read_events(root)).batches["b-001"]["attempts"]["futile_pushes"] == 0


# --- issue #62: an interrupted build has to be resumable ---------------------


def test_re_entering_building_is_allowed_so_an_interrupted_build_can_resume(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    ledger.transition(root, "b-001", "building")
    before = ledger.fold(ledger.read_events(root)).batches["b-001"]["progress_at"]
    ledger.transition(root, "b-001", "building")
    batch = ledger.fold(ledger.read_events(root)).batches["b-001"]
    assert batch["state"] == "building"
    assert batch["progress_at"] == before, "restarting is not progress"


def test_each_resume_of_an_interrupted_build_is_counted(root):
    """`building -> building` records no progress, so a counter is the only
    thing that can tell one resume from eleven."""
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    for _ in range(4):
        ledger.transition(root, "b-001", "building")
    attempts = ledger.fold(ledger.read_events(root)).batches["b-001"]["attempts"]
    assert attempts["build_resumes"] == 3, "the first entry is the build, not a resume"


def test_starting_a_build_is_not_a_resume(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    ledger.transition(root, "b-001", "building")
    assert ledger.fold(ledger.read_events(root)).batches["b-001"]["attempts"]["build_resumes"] == 0


def test_a_build_picked_up_past_the_ceiling_is_reported_as_stalled():
    batch = {"state": "building", "attempts": {"build_resumes": 3}}
    reason = ledger.stalled_build(batch, {})
    assert reason and "3" in reason


def test_a_build_below_the_ceiling_is_still_the_loops_own_problem():
    assert ledger.stalled_build({"state": "building", "attempts": {"build_resumes": 2}}, {}) is None


def test_a_batch_that_got_out_of_building_is_not_stalled_by_its_old_resumes():
    """The resumes happened, but the build finished; the count is history now."""
    batch = {"state": "open", "attempts": {"build_resumes": 9}}
    assert ledger.stalled_build(batch, {}) is None


def test_the_resume_ceiling_can_be_tightened_from_the_config():
    batch = {"state": "building", "attempts": {"build_resumes": 1}}
    assert ledger.stalled_build(batch, {"build_resumes": 1})


def test_a_batch_with_no_resume_counter_at_all_is_not_stalled():
    assert ledger.stalled_build({"state": "building"}, {}) is None


# --- issue #64: a ledger path must not follow the caller around --------------


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=f@example.com", "-c", "user.name=foreman", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def worktree(tmp_path):
    """A repo plus the build worktree `commands/build.md` prescribes.

    Returns (checkout, worktree). The whole build and push happens with the cwd
    inside the worktree, which is where the second ledger used to appear.
    """
    checkout = tmp_path / "repo"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "root")
    linked = tmp_path / "foreman-b-002"
    _git(checkout, "worktree", "add", "-q", str(linked), "-b", "foreman/b-002")
    return checkout, linked


def test_a_write_from_a_build_worktree_lands_in_the_real_ledger(worktree, monkeypatch):
    checkout, linked = worktree
    ledger.init(checkout)
    monkeypatch.chdir(linked)
    ledger.append(Path(ledger.LEDGER_DIR), "batch.pushed", batch="b-002", sha="abc123")
    assert not (linked / ledger.LEDGER_DIR).exists(), "no second ledger in the worktree"
    events = ledger.read_events(checkout / ledger.LEDGER_DIR)
    assert [e["type"] for e in events] == ["batch.pushed"]


def test_a_read_from_a_build_worktree_sees_the_real_ledger(worktree, monkeypatch):
    checkout, linked = worktree
    root = ledger.init(checkout)
    ledger.append(root, "batch.created", batch="b-002", issues=[1])
    monkeypatch.chdir(linked)
    assert "b-002" in ledger.load(Path(ledger.LEDGER_DIR)).batches


def test_init_from_a_build_worktree_does_not_create_a_second_ledger(worktree, monkeypatch):
    checkout, linked = worktree
    monkeypatch.chdir(linked)
    ledger.init(Path("."))
    assert (checkout / ledger.LEDGER_DIR).is_dir()
    assert not (linked / ledger.LEDGER_DIR).exists()


def test_an_explicit_ledger_path_still_wins(worktree, monkeypatch, tmp_path):
    checkout, linked = worktree
    elsewhere = ledger.init(tmp_path / "elsewhere")
    monkeypatch.chdir(linked)
    ledger.append(elsewhere, "batch.pushed", batch="b-002", sha="abc123")
    assert len(ledger.read_events(elsewhere)) == 1
    assert not (checkout / ledger.LEDGER_DIR).exists()


def test_a_directory_outside_any_repo_still_works(tmp_path, monkeypatch):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)
    ledger.init(Path("."))
    ledger.append(Path(ledger.LEDGER_DIR), "issue.triaged", issue=1, verdict="actionable")
    assert len(ledger.read_events(outside / ledger.LEDGER_DIR)) == 1


# --- issue #70: a config path must not follow the caller around either -------


def test_a_relative_config_path_resolves_against_the_repository(worktree, monkeypatch):
    """The default `.foreman/config.json` names the repo's config, not the caller's."""
    checkout, linked = worktree
    root = ledger.init(checkout)
    (root / ledger.CONFIG_FILE).write_text(json.dumps({"caps": {"pushes": 8}}))
    monkeypatch.chdir(linked)
    assert ledger.load_config(None)["caps"]["pushes"] == 8
    assert ledger.load_config(f"{ledger.LEDGER_DIR}/{ledger.CONFIG_FILE}")["caps"]["pushes"] == 8


def test_an_explicit_absolute_config_path_still_wins(worktree, monkeypatch, tmp_path):
    """An absolute path is how a caller says "this config, not the one you'd pick"."""
    checkout, linked = worktree
    elsewhere = tmp_path / "elsewhere.json"
    elsewhere.write_text(json.dumps({"risk_ceiling": "low"}))
    ledger.init(checkout)
    (checkout / ledger.LEDGER_DIR / ledger.CONFIG_FILE).write_text(
        json.dumps({"risk_ceiling": "high"})
    )
    monkeypatch.chdir(linked)
    assert ledger.load_config(elsewhere)["risk_ceiling"] == "low"


def test_a_missing_config_says_so_rather_than_defaulting_to_no_limits(
    worktree, monkeypatch, capsys
):
    """Silence was the whole defect: every cap and limit disappeared without a word."""
    checkout, linked = worktree
    monkeypatch.chdir(linked)
    assert ledger.load_config(None) == {}
    err = capsys.readouterr().err
    assert str(checkout / ledger.LEDGER_DIR / ledger.CONFIG_FILE) in err
    assert "cap" in err.lower()


def test_a_config_that_is_there_is_read_without_complaint(worktree, monkeypatch, capsys):
    checkout, linked = worktree
    root = ledger.init(checkout)
    (root / ledger.CONFIG_FILE).write_text(json.dumps({"auto_merge": True}))
    monkeypatch.chdir(linked)
    assert ledger.load_config(None) == {"auto_merge": True}
    assert capsys.readouterr().err == ""


def test_a_config_in_a_directory_outside_any_repository_still_resolves(tmp_path, monkeypatch):
    outside = tmp_path / "not-a-repo"
    (outside / ledger.LEDGER_DIR).mkdir(parents=True)
    (outside / ledger.LEDGER_DIR / ledger.CONFIG_FILE).write_text(json.dumps({"auto_merge": True}))
    monkeypatch.chdir(outside)
    assert ledger.load_config(None) == {"auto_merge": True}
