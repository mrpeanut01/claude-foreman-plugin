"""Landing: read CI honestly, judge the reviewer, and refuse to merge on doubt."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import land  # noqa: E402
import ledger  # noqa: E402

PROFILE = {
    "required_checks": ["lint", "unit", "integration"],
    "protection_known": True,
    "jobs": {
        "lint": {"tier": "cheap", "required": True},
        "unit": {"tier": "cheap", "required": True},
        "integration": {"tier": "expensive", "required": True},
        "codecov": {"tier": "cheap", "required": False},
    },
}


def check(name, state, description=""):
    return {"name": name, "state": state, "description": description}


# --- reading the check list ---------------------------------------------------


def test_a_pending_required_check_is_actionable():
    s = land.classify_checks([check("lint", "SUCCESS"), check("unit", "PENDING")], PROFILE)
    assert s["actionable_pending"] == ["unit"] and s["failed"] == []


def test_a_pending_advisory_check_is_not_worth_waiting_for():
    s = land.classify_checks([check("lint", "SUCCESS"), check("codecov", "PENDING")], PROFILE)
    assert s["actionable_pending"] == []
    assert "codecov" in s["advisory_pending"]


def test_a_human_approval_gate_is_never_waited_on():
    """Sentry's rule: the loop must not block on something only a person can do."""
    s = land.classify_checks([check("unit", "PENDING", "Waiting for approval")], PROFILE)
    assert s["actionable_pending"] == []
    assert s["human_gate_pending"] == ["unit"]


def test_a_failed_advisory_check_does_not_count_as_a_failure():
    s = land.classify_checks([check("codecov", "FAILURE"), check("lint", "SUCCESS")], PROFILE)
    assert s["failed"] == [] and "codecov" in s["advisory_failed"]


def test_a_check_the_profile_has_never_seen_is_treated_as_required():
    """An unknown check blocking the merge queue must not be optimised away."""
    s = land.classify_checks([check("new-gate", "PENDING")], PROFILE)
    assert s["actionable_pending"] == ["new-gate"]


# --- deriving the CI gate -----------------------------------------------------


def test_all_required_green_is_full_green():
    checks = [check(n, "SUCCESS") for n in ("lint", "unit", "integration")]
    assert land.ci_gate(checks, PROFILE) == "full_green"


def test_cheap_green_while_the_expensive_tier_has_not_run():
    checks = [check("lint", "SUCCESS"), check("unit", "SUCCESS"), check("integration", "PENDING")]
    assert land.ci_gate(checks, PROFILE) == "cheap_green"


def test_any_required_failure_fails_the_gate():
    checks = [check("lint", "SUCCESS"), check("unit", "FAILURE"), check("integration", "SUCCESS")]
    assert land.ci_gate(checks, PROFILE) == "failed"


def test_nothing_reported_yet_is_pending_not_green():
    assert land.ci_gate([], PROFILE) == "pending"


def test_a_cheap_check_still_running_is_not_cheap_green():
    checks = [check("lint", "PENDING"), check("unit", "SUCCESS")]
    assert land.ci_gate(checks, PROFILE) == "pending"


# --- judging the reviewer -----------------------------------------------------


def clean(**kw):
    base = {
        "verdict": "clean",
        "tests_covering": ["tests/test_upload.py::test_503_retry"],
        "revert_check": "failed_as_expected",
        "findings": [],
        "behaviour_change": True,
    }
    return {**base, **kw}


def test_a_clean_verdict_with_evidence_is_accepted():
    ok, errors = land.validate_review(clean())
    assert ok and errors == []


def test_a_clean_verdict_naming_no_covering_test_is_rejected():
    ok, errors = land.validate_review(clean(tests_covering=[]))
    assert not ok and any("tests_covering" in e for e in errors)


def test_a_test_that_still_passes_after_reverting_the_fix_guards_nothing():
    ok, errors = land.validate_review(clean(revert_check="still_passed"))
    assert not ok and any("revert" in e.lower() for e in errors)


def test_a_clean_verdict_carrying_a_serious_finding_contradicts_itself():
    ok, errors = land.validate_review(clean(findings=[{"severity": "high", "summary": "race"}]))
    assert not ok and any("finding" in e.lower() for e in errors)


def test_a_low_severity_note_does_not_block_a_clean_verdict():
    ok, _ = land.validate_review(clean(findings=[{"severity": "low", "summary": "naming"}]))
    assert ok


def test_changes_requested_needs_no_evidence():
    ok, errors = land.validate_review(
        {
            "verdict": "changes_requested",
            "findings": [{"severity": "high", "summary": "off by one"}],
        }
    )
    assert ok and errors == []


def test_changes_requested_with_no_finding_is_rejected():
    ok, errors = land.validate_review({"verdict": "changes_requested", "findings": []})
    assert not ok and any("finding" in e.lower() for e in errors)


def test_a_docs_only_change_may_skip_the_revert_check():
    ok, _ = land.validate_review(
        clean(revert_check="not_applicable", tests_covering=[], behaviour_change=False)
    )
    assert ok


def test_a_behaviour_change_may_not_skip_the_revert_check():
    ok, errors = land.validate_review(clean(revert_check="not_applicable"))
    assert not ok and any("revert" in e.lower() for e in errors)


def test_an_unrecognised_verdict_is_rejected_rather_than_guessed():
    ok, errors = land.validate_review({"verdict": "looks fine to me"})
    assert not ok and any("verdict" in e.lower() for e in errors)


def test_a_missing_verdict_is_rejected():
    ok, errors = land.validate_review({})
    assert not ok


# --- flake or bug -------------------------------------------------------------

CAPS = {"caps": {"reruns": 2}}


@pytest.mark.parametrize(
    "classification,reruns,expected",
    [
        ({"is_flaky": True, "confidence": 0.9}, 0, "rerun"),
        ({"is_flaky": True, "confidence": 0.7}, 1, "rerun"),
        ({"is_flaky": True, "confidence": 0.55}, 0, "fix"),  # below the bar: treat as real
        ({"is_flaky": False, "confidence": 0.99}, 0, "fix"),
        ({"is_flaky": True, "confidence": 0.95}, 2, "escalate"),  # at the rerun cap
    ],
)
def test_flake_decision(classification, reruns, expected):
    batch = {"attempts": {"pushes": 0, "review_rounds": 0, "reruns": reruns}}
    assert land.flake_decision(classification, batch, CAPS) == expected


def test_a_classification_with_no_confidence_is_treated_as_real():
    batch = {"attempts": {"reruns": 0}}
    assert land.flake_decision({"is_flaky": True}, batch, CAPS) == "fix"


# --- the merge decision -------------------------------------------------------


CONFIRMED = "c" * 40


def ready_batch(**kw):
    base = {
        "id": "b-001",
        "state": "ready",
        "ci_gate": "full_green",
        "review_gate": "clean",
        "attempts": {"pushes": 1, "review_rounds": 0, "reruns": 0},
        "paths": ["src/upload.py"],
        # The paths were checked against the branch's diff, at this commit.
        "paths_head": CONFIRMED,
    }
    return {**base, **kw}


CFG = {
    "auto_merge": True,
    "caps": {"pushes": 3, "review_rounds": 2, "reruns": 2},
    "protected_paths": ["**/auth/**", "**/migrations/**"],
}


def test_a_clean_batch_has_no_blockers():
    assert land.merge_blockers(ready_batch(), {"labels": []}, CFG) == []


def test_a_pending_gate_blocks_the_merge():
    blockers = land.merge_blockers(ready_batch(review_gate="pending"), {"labels": []}, CFG)
    assert any("review" in b for b in blockers)


def test_a_needs_human_label_blocks_regardless_of_green():
    blockers = land.merge_blockers(ready_batch(), {"labels": ["needs-human"]}, CFG)
    assert any("needs-human" in b for b in blockers)


def test_a_protected_path_is_never_auto_merged():
    blockers = land.merge_blockers(ready_batch(paths=["src/auth/session.py"]), {"labels": []}, CFG)
    assert any("auth" in b for b in blockers)


def test_a_cap_breach_blocks_the_merge():
    batch = ready_batch(attempts={"pushes": 3, "review_rounds": 0, "reruns": 0})
    assert any("pushes" in b for b in land.merge_blockers(batch, {"labels": []}, CFG))


def test_auto_merge_off_blocks_everything():
    blockers = land.merge_blockers(ready_batch(), {"labels": []}, {**CFG, "auto_merge": False})
    assert any("auto_merge" in b for b in blockers)


def test_blockers_are_reported_together_not_one_at_a_time():
    batch = ready_batch(review_gate="pending", paths=["src/auth/x.py"])
    assert len(land.merge_blockers(batch, {"labels": ["do-not-merge"]}, CFG)) >= 3


# --- issue #1: absent branch protection must not read as "nothing is required" -


def job(**kw):
    """Shaped like what parse_workflows actually emits, triggers included."""
    base = {
        "tier": "cheap",
        "required": False,
        "display": None,
        "triggers": ["pull_request", "push"],
        "path_filters": [],
    }
    return {**base, **kw}


UNPROTECTED = {
    "required_checks": [],
    "protection_known": False,
    "jobs": {"lint": job(), "test": job()},
}


def test_a_red_ci_is_never_green_when_protection_is_unknown():
    red = [check("lint", "FAILURE"), check("test", "FAILURE")]
    assert land.classify_checks(red, UNPROTECTED)["failed"] == ["lint", "test"]
    assert land.ci_gate(red, UNPROTECTED) == "failed"


def test_unknown_protection_makes_every_check_required():
    mixed = [check("lint", "SUCCESS"), check("test", "PENDING")]
    assert land.classify_checks(mixed, UNPROTECTED)["actionable_pending"] == ["test"]
    assert land.ci_gate(mixed, UNPROTECTED) == "pending"


def test_all_green_under_unknown_protection_is_full_green():
    green = [check("lint", "SUCCESS"), check("test", "SUCCESS")]
    assert land.ci_gate(green, UNPROTECTED) == "full_green"


def test_advisory_still_means_advisory_when_protection_is_known():
    known = {**UNPROTECTED, "protection_known": True, "required_checks": ["lint"]}
    assert land.classify_checks([check("test", "FAILURE")], known)["failed"] == []


def test_an_unprotected_repo_with_red_ci_cannot_merge():
    red_batch = {
        "id": "b-001",
        "ci_gate": land.ci_gate([check("lint", "FAILURE")], UNPROTECTED),
        "review_gate": "clean",
        "paths": ["scripts/x.py"],
        "attempts": {"pushes": 1, "review_rounds": 0, "reruns": 0},
    }
    blockers = land.merge_blockers(
        red_batch, {"labels": []}, {"auto_merge": True, "caps": {}, "protected_paths": []}
    )
    assert blockers, "a fully red CI must block the merge"


def test_a_pr_with_no_checks_yet_is_not_green_when_protection_is_unknown():
    """Otherwise a freshly pushed PR merges having run zero CI."""
    assert land.ci_gate([], UNPROTECTED) == "pending"


def test_no_required_checks_under_known_protection_is_a_deliberate_choice():
    known = {"required_checks": [], "protection_known": True, "jobs": {}}
    assert land.ci_gate([], known) == "full_green"


# --- convergence, not rounds elapsed -----------------------------------------
# The cap was justified as "a third round of an agent negotiating with an agent
# is not going to converge". Rounds elapsed does not measure that. Findings that
# survive a round do.


def finding(file, severity="medium", summary="the retry loop has no ceiling"):
    return {"file": file, "severity": severity, "summary": summary}


def test_a_finding_that_survives_a_round_means_no_convergence():
    rounds = [[finding("src/a.py")], [finding("src/a.py")]]
    assert "repeat" in land.review_stalled(rounds, hard_ceiling=5).lower()


def test_different_findings_each_round_is_progress_not_deadlock():
    rounds = [
        [finding("src/a.py", summary="retry loop has no ceiling")],
        [finding("src/b.py", summary="token vocabulary lost its plurals")],
    ]
    assert land.review_stalled(rounds, hard_ceiling=5) is None


def test_rewording_the_same_finding_still_counts_as_a_repeat():
    rounds = [
        [finding("src/a.py", summary="the retry loop has no ceiling")],
        [finding("src/a.py", summary="retry loop has no upper ceiling at all")],
    ]
    assert land.review_stalled(rounds, hard_ceiling=5) is not None


def test_the_same_file_at_a_different_severity_is_a_different_finding():
    rounds = [[finding("src/a.py", severity="high")], [finding("src/a.py", severity="low")]]
    assert land.review_stalled(rounds, hard_ceiling=5) is None


def test_a_repeated_low_finding_does_not_stall_the_review():
    """Low findings never block a clean verdict, so repeating one is not deadlock."""
    rounds = [[finding("src/a.py", severity="low")], [finding("src/a.py", severity="low")]]
    assert land.review_stalled(rounds, hard_ceiling=5) is None


def test_only_consecutive_rounds_are_compared():
    rounds = [[finding("src/a.py")], [finding("src/b.py")], [finding("src/a.py")]]
    assert land.review_stalled(rounds, hard_ceiling=5) is None


def test_a_hard_ceiling_still_bounds_an_unattended_loop():
    rounds = [[finding(f"src/{n}.py")] for n in range(5)]
    reason = land.review_stalled(rounds, hard_ceiling=5)
    assert reason and "ceiling" in reason.lower()


def test_one_round_is_never_stalled():
    assert land.review_stalled([[finding("src/a.py")]], hard_ceiling=5) is None


def test_no_rounds_is_never_stalled():
    assert land.review_stalled([], hard_ceiling=5) is None


def test_a_clean_round_after_findings_is_not_a_repeat():
    rounds = [[finding("src/a.py")], []]
    assert land.review_stalled(rounds, hard_ceiling=5) is None


def test_the_stall_reason_names_the_finding_that_survived():
    rounds = [
        [finding("scripts/triage.py", summary="auth vocabulary is incomplete")],
        [finding("scripts/triage.py", summary="the auth vocabulary is still incomplete")],
    ]
    assert "scripts/triage.py" in land.review_stalled(rounds, hard_ceiling=5)


# --- issue #12: a partial check list is not a complete one --------------------


def test_a_partially_reported_check_list_is_not_green_when_protection_is_unknown():
    """`test` needs `lint`; the window after lint passes and before test registers."""
    only_lint = [check("lint", "SUCCESS")]
    assert land.ci_gate(only_lint, UNPROTECTED) == "pending"


def test_every_declared_job_must_report_before_green_under_unknown_protection():
    partial = [check("lint", "SUCCESS"), check("test", "PENDING")]
    assert land.ci_gate(partial, UNPROTECTED) == "pending"


def test_matrix_cells_satisfy_the_job_they_belong_to():
    """Otherwise the fix above would deadlock every matrix repo."""
    profile = {
        "required_checks": [],
        "protection_known": False,
        "jobs": {"lint": job(), "test": job()},
    }
    matrix = [
        check("lint", "SUCCESS"),
        check("test (3.11)", "SUCCESS"),
        check("test (3.12)", "SUCCESS"),
        check("test (3.13)", "SUCCESS"),
    ]
    assert land.ci_gate(matrix, profile) == "full_green"


def test_a_profile_declaring_no_jobs_is_never_green_on_an_unrelated_check():
    """Superseded by tests/test_gate_spec.py: an unknown check proves nothing."""
    bare = {"required_checks": [], "protection_known": False, "jobs": {}}
    assert land.ci_gate([check("something", "SUCCESS")], bare) == "pending"


# --- issue #18: a job that cannot report must not pin the gate ---------------


def _unprotected_with(**extra_jobs):
    return {
        "required_checks": [],
        "protection_known": False,
        "jobs": {"lint": job(), "test": job(), **extra_jobs},
    }


GREEN = [check("lint", "SUCCESS"), check("test", "SUCCESS")]


def test_a_schedule_only_job_does_not_block_a_pull_request():
    profile = _unprotected_with(nightly=job(triggers=["schedule"]))
    assert land.ci_gate(GREEN, profile) == "full_green"


def test_a_dispatch_only_job_does_not_block():
    profile = _unprotected_with(release=job(triggers=["workflow_dispatch"]))
    assert land.ci_gate(GREEN, profile) == "full_green"


def test_a_path_filtered_job_that_did_not_report_does_not_block():
    profile = _unprotected_with(docs=job(path_filters=["docs/**"]))
    assert land.ci_gate(GREEN, profile) == "full_green"


def test_a_job_whose_name_is_a_template_cannot_be_attributed_so_cannot_be_required():
    profile = _unprotected_with(e2e=job(display="E2E ${{ matrix.browser }}"))
    assert land.ci_gate(GREEN, profile) == "full_green"


def test_an_unconditional_job_that_never_reported_still_blocks():
    """The #12 fix must survive: a plain PR job going missing is not fine."""
    profile = _unprotected_with(build=job())
    assert land.ci_gate(GREEN, profile) == "pending"


def test_a_conditional_job_that_did_report_and_failed_still_fails_the_gate():
    profile = _unprotected_with(docs=job(path_filters=["docs/**"]))
    assert land.ci_gate([*GREEN, check("docs", "FAILURE")], profile) == "failed"


# --- issue #24: a severity that drifts must not defeat the repeat detector ---


def test_a_finding_alternating_between_blocking_severities_is_still_a_repeat():
    a = {
        "file": "scripts/triage.py",
        "severity": "high",
        "summary": "the auth vocabulary is still incomplete",
    }
    b = {**a, "severity": "medium"}
    assert land.review_stalled([[a], [b]], hard_ceiling=5) is not None


def test_a_downgrade_out_of_the_blocking_band_is_still_progress():
    a = {"file": "scripts/triage.py", "severity": "high", "summary": "vocabulary incomplete"}
    b = {**a, "severity": "low"}
    assert land.review_stalled([[a], [b]], hard_ceiling=5) is None


# --- issue #25: a running job still holds the gate, requirable or not --------


def test_a_non_requirable_job_that_is_actually_running_still_holds_the_gate():
    profile = _unprotected_with(integration=job(path_filters=["src/**"]))
    checks = [*GREEN, check("integration", "PENDING")]
    assert land.classify_checks(checks, profile)["actionable_pending"] == ["integration"]
    assert land.ci_gate(checks, profile) == "pending"


def test_a_templated_matrix_job_that_is_running_still_holds_the_gate():
    profile = _unprotected_with(e2e=job(display="E2E ${{ matrix.browser }}"))
    assert land.ci_gate([*GREEN, check("E2E chrome", "PENDING")], profile) == "pending"


# --- issue #26: path filters are per event ----------------------------------


def test_a_push_only_path_filter_does_not_excuse_a_pull_request_job():
    """`on: push: {paths: [...]}` plus an unconditional `pull_request` trigger."""
    spec = job(path_filters=["src/**"], pr_path_filters=[])
    assert land._can_report(spec) is True


def test_a_pull_request_path_filter_still_makes_a_job_conditional():
    spec = job(path_filters=["docs/**"], pr_path_filters=["docs/**"])
    assert land._can_report(spec) is False


# --- issue #8: a reported name must be resolved to the job that declared it ---

# Branch protection names the contexts GitHub actually reports. For a matrix job
# that is one context per cell — never the workflow's job key, which is what the
# profile is keyed by. `e2e` therefore carries required=False even though both of
# its cells are required to merge.
MATRIX = {
    "required_checks": ["lint", "e2e (chrome)", "e2e (firefox)"],
    "protection_known": True,
    "jobs": {
        "lint": {"tier": "cheap", "required": True, "display": None},
        "e2e": {"tier": "expensive", "required": False, "display": None},
        "coverage": {"tier": "cheap", "required": False, "display": None},
    },
}


def test_a_matrix_cell_inherits_the_tier_of_the_job_that_declared_it():
    """Otherwise the expensive tier is invisible and gets paid for on every push."""
    checks = [
        check("lint", "SUCCESS"),
        check("e2e (chrome)", "PENDING"),
        check("e2e (firefox)", "PENDING"),
    ]
    assert land.ci_gate(checks, MATRIX) == "cheap_green"


def test_a_matrix_cell_of_an_advisory_job_is_advisory_too():
    s = land.classify_checks([check("coverage (3.11)", "FAILURE")], MATRIX)
    assert s["failed"] == [] and s["advisory_failed"] == ["coverage (3.11)"]


def test_a_required_context_is_never_advisory_whatever_its_job_key_says():
    """`e2e` reads required=False; the cell protection names is still required."""
    s = land.classify_checks([check("e2e (chrome)", "FAILURE")], MATRIX)
    assert s["failed"] == ["e2e (chrome)"]
    assert land.ci_gate([check("e2e (chrome)", "FAILURE")], MATRIX) == "failed"


# --- issue #9: a gate verdict is a statement about one commit ----------------
# The ledger resets both gates on `batch.pushed` for exactly this reason. If the
# check list read straight afterwards still describes the previous commit, the
# reset buys nothing.

NEW = "a" * 40
OLD = "b" * 40


def sha_check(name, state, sha, description=""):
    return {"name": name, "state": state, "description": description, "head_sha": sha}


def test_checks_from_the_previous_commit_do_not_make_the_new_one_green():
    stale = [sha_check(n, "SUCCESS", OLD) for n in ("lint", "unit", "integration")]
    assert land.ci_gate(stale, PROFILE, expected_sha=NEW) == "pending"


def test_checks_carrying_no_commit_at_all_cannot_prove_anything():
    """`gh pr checks` output has no head SHA, so it can never be attributed."""
    unattributable = [check(n, "SUCCESS") for n in ("lint", "unit", "integration")]
    assert land.ci_gate(unattributable, PROFILE, expected_sha=NEW) == "pending"


def test_checks_for_the_commit_being_gated_are_read_normally():
    fresh = [sha_check(n, "SUCCESS", NEW) for n in ("lint", "unit", "integration")]
    assert land.ci_gate(fresh, PROFILE, expected_sha=NEW) == "full_green"


def test_a_failure_on_the_previous_commit_does_not_fail_the_new_one():
    """Stale evidence is evidence of nothing, in either direction."""
    mixed = [sha_check("lint", "FAILURE", OLD), sha_check("lint", "SUCCESS", NEW)]
    s = land.classify_checks(mixed, PROFILE, expected_sha=NEW)
    assert s["failed"] == [] and s["passed"] == ["lint"] and s["stale"] == ["lint"]


def test_an_abbreviated_sha_still_identifies_the_commit():
    fresh = [sha_check(n, "SUCCESS", NEW) for n in ("lint", "unit", "integration")]
    assert land.ci_gate(fresh, PROFILE, expected_sha=NEW[:7]) == "full_green"


def test_asking_for_no_particular_commit_keeps_the_unscoped_reading():
    green = [check(n, "SUCCESS") for n in ("lint", "unit", "integration")]
    assert land.ci_gate(green, PROFILE) == "full_green"
    assert land.classify_checks(green, PROFILE)["stale"] == []


def test_fetching_checks_for_a_sha_uses_the_sha_addressed_endpoints(monkeypatch):
    calls = []

    def fake_gh(args):
        calls.append(" ".join(args))
        if "check-runs" in args[-1]:
            return {
                "check_runs": [
                    {
                        "name": "test (3.11)",
                        "status": "completed",
                        "conclusion": "success",
                        "head_sha": NEW,
                        "output": {"title": "3 passed"},
                        "html_url": "https://example/run",
                    }
                ]
            }
        return {
            "statuses": [
                {
                    "context": "buildkite",
                    "state": "pending",
                    "description": "waiting for agent",
                    "target_url": "https://example/status",
                }
            ]
        }

    monkeypatch.setattr(land, "_gh_json", fake_gh)
    entries = land.fetch_checks("o/r", 7, sha=NEW)

    assert all(f"commits/{NEW}" in call for call in calls), calls
    assert {e["name"] for e in entries} == {"test (3.11)", "buildkite"}
    assert {e["state"] for e in entries} == {"SUCCESS", "PENDING"}
    # Every entry must carry the commit it describes, or the gate cannot check it.
    assert all(e["head_sha"] == NEW for e in entries)


def test_fetching_checks_with_no_sha_still_reads_the_pull_request(monkeypatch):
    monkeypatch.setattr(land, "_gh_json", lambda args: [{"name": "lint", "state": "SUCCESS"}])
    assert land.fetch_checks("o/r", 7) == [{"name": "lint", "state": "SUCCESS"}]


# --- issue #29: two defects in one file are not one finding repeating --------


def test_two_different_defects_in_one_file_are_not_one_repeated_finding():
    """Half the content words are the locus and the verb; the noun is the defect."""
    a = finding("scripts/parse.py", "high", "missing null check in parse_config")
    b = finding("scripts/parse.py", "medium", "missing type check in parse_config")
    assert land.same_finding(a, b) is False
    assert land.review_stalled([[a], [b]], hard_ceiling=5) is None


def test_a_repeat_that_only_adds_words_is_still_a_repeat():
    """A rewording elaborates; it does not name something new."""
    a = finding("scripts/parse.py", "high", "missing null check in parse_config")
    b = finding("scripts/parse.py", "high", "still a missing null check in parse_config on line 4")
    assert land.same_finding(a, b) is True


def test_two_findings_naming_different_things_are_different_however_much_they_share():
    a = finding("src/pool.py", "high", "connection pool leaks handles on timeout")
    b = finding("src/pool.py", "high", "connection pool leaks handles on shutdown")
    assert land.same_finding(a, b) is False


# --- issue #36: a locus wrong round after round is deadlock, however worded --


def locus_rounds(*summaries, file="scripts/land.py", severity="high"):
    return [[finding(file, severity, summary)] for summary in summaries]


ARC = (
    "empty check list read as full_green",
    "requiring every declared job hangs on unreportable ones",
    "a running job is invisible, so the gate merges early",
)


def test_one_file_wrong_in_three_consecutive_rounds_is_a_deadlock():
    """PR #7's own arc: one function, four rounds, alternating directions."""
    reason = land.review_stalled(locus_rounds(*ARC), hard_ceiling=5)
    assert reason and "locus" in reason.lower() and "scripts/land.py" in reason


def test_two_rounds_on_one_file_is_ordinary_iteration():
    assert land.review_stalled(locus_rounds(*ARC[:2]), hard_ceiling=5) is None


def test_three_rounds_spread_across_files_is_progress():
    rounds = [
        [finding("scripts/land.py", "high", ARC[0])],
        [finding("scripts/triage.py", "high", "auth vocabulary is incomplete")],
        [finding("scripts/loop.py", "high", "the daily budget never refreshes")],
    ]
    assert land.review_stalled(rounds, hard_ceiling=5) is None


def test_a_round_that_clears_the_file_breaks_the_run():
    rounds = [
        [finding("scripts/land.py", "high", ARC[0])],
        [finding("scripts/loop.py", "high", "the daily budget never refreshes")],
        [finding("scripts/land.py", "high", ARC[2])],
    ]
    assert land.review_stalled(rounds, hard_ceiling=5) is None


def test_repeated_low_findings_in_one_file_are_not_a_deadlock():
    """A low finding never blocked a merge, so repeating one blocks nothing."""
    assert land.review_stalled(locus_rounds(*ARC, severity="low"), hard_ceiling=5) is None


def test_the_locus_run_survives_the_file_being_named_alongside_others():
    rounds = [
        [finding("scripts/land.py", "high", ARC[0]), finding("scripts/loop.py", "high", "a")],
        [finding("scripts/land.py", "high", ARC[1])],
        [finding("scripts/triage.py", "high", "b"), finding("scripts/land.py", "high", ARC[2])],
    ]
    assert "scripts/land.py" in land.review_stalled(rounds, hard_ceiling=5)


# --- issue #49: firing on some pull requests is not firing on every one ------


def event_job(**events):
    """Shaped like a profile entry with per-event data, as parse_workflows emits."""
    return {
        "events": events,
        "display": None,
        "tier": "cheap",
        "required": False,
        "triggers": sorted(events),
        "path_filters": [],
        "pr_path_filters": [],
    }


def test_a_job_gated_on_ready_for_review_alone_is_not_requirable():
    """The standard way to hold an E2E suite until a PR leaves draft. foreman
    opens non-draft PRs, so the event never fires and no check is ever created."""
    assert (
        land.can_report_on_pr(event_job(pull_request={"types": ["ready_for_review"]}), "main")
        is False
    )


def test_a_job_gated_on_edited_alone_is_not_requirable():
    assert land.can_report_on_pr(event_job(pull_request={"types": ["edited"]}), "main") is False


def test_the_default_activity_types_still_make_a_job_requirable():
    types = {"types": ["opened", "synchronize", "reopened"]}
    assert land.can_report_on_pr(event_job(pull_request=types), "main") is True


def test_a_ready_for_review_job_does_not_hold_the_gate_open_forever():
    profile = {
        "required_checks": [],
        "protection_known": False,
        "jobs": {
            "lint": event_job(pull_request={}),
            "e2e": event_job(pull_request={"types": ["ready_for_review"]}),
        },
    }
    assert land.ci_gate([check("lint", "SUCCESS")], profile, "main") == "full_green"


def test_a_ready_for_review_job_that_does_report_is_still_waited_on():
    """Not requirable is not ignorable: a check that appears still holds the gate."""
    profile = {
        "required_checks": [],
        "protection_known": False,
        "jobs": {
            "lint": event_job(pull_request={}),
            "e2e": event_job(pull_request={"types": ["ready_for_review"]}),
        },
    }
    checks = [check("lint", "SUCCESS"), check("e2e", "PENDING")]
    assert land.ci_gate(checks, profile, "main") == "pending"


# --- issue #70: the config must not vanish when land runs from a worktree -----


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=f@example.com", "-c", "user.name=foreman", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def worktree(tmp_path):
    """The layout `commands/build.md` prescribes: the config lives one repo up."""
    checkout = tmp_path / "repo"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "root")
    linked = tmp_path / "foreman-b-001"
    _git(checkout, "worktree", "add", "-q", str(linked), "-b", "foreman/b-001")

    import ledger

    root = ledger.init(checkout)
    (root / ledger.CONFIG_FILE).write_text(json.dumps(CFG))
    # A batch whose only remaining objection is that it touches a protected path.
    ledger.append(root, "batch.created", batch="b-001", issues=[1], paths=["src/auth/session.py"])
    for state in ("building", "built", "open"):
        ledger.transition(root, "b-001", state)
    ledger.gate(root, "b-001", "ci", "full_green")
    ledger.gate(root, "b-001", "review", "clean")
    ledger.transition(root, "b-001", "ready")
    return checkout, linked


def test_land_reads_its_protected_paths_from_the_repository_when_run_from_a_worktree(
    worktree, monkeypatch, capsys
):
    """Merging itself fails safe with no config — `auto_merge` defaults off — but the
    protected-path objection disappears entirely, so nothing records *why* the batch
    should never have auto-merged."""
    checkout, linked = worktree
    monkeypatch.chdir(linked)

    assert land.main(["blockers", "--batch", "b-001"]) == 2
    blockers = json.loads(capsys.readouterr().out)["blockers"]
    assert any("src/auth/session.py is protected" in b for b in blockers)
    assert "auto_merge is disabled in config" not in blockers


def test_checks_reads_its_profile_from_the_repository_when_run_from_a_worktree(
    worktree, monkeypatch, capsys
):
    """Issue #74. Read against the caller, the profile was simply not there from a
    build worktree, and an advisory job with no profile to say so is an unknown
    check — which counts as required, so its red failed the gate."""
    checkout, linked = worktree
    import ledger

    (checkout / ledger.LEDGER_DIR / ledger.PROFILE_FILE).write_text(
        json.dumps(
            {
                "protection_known": True,
                "required_checks": [],
                "jobs": {"lint": {"tier": "cheap", "required": False}},
            }
        )
    )

    def fake_gh(args):
        if args[:2] == ["pr", "view"]:
            return {"number": 7, "headRefOid": NEW, "baseRefName": "main", "labels": []}
        if "check-runs" in args[-1]:
            return {
                "check_runs": [
                    {
                        "name": "lint",
                        "status": "completed",
                        "conclusion": "failure",
                        "head_sha": NEW,
                    }
                ]
            }
        return {"statuses": []}

    monkeypatch.setattr(land, "_gh_json", fake_gh)
    monkeypatch.chdir(linked)

    land.main(["checks", "--pr", "7", "--repo", "o/r"])
    out = json.loads(capsys.readouterr().out)
    assert out["advisory_failed"] == ["lint"]
    assert out["failed"] == []


def test_land_says_so_when_it_is_judging_a_merge_with_no_config(worktree, monkeypatch, capsys):
    """Merging itself fails safe here — `auto_merge` defaults off — so this is not an
    auto-merge hole. What is missing is the reason: the protected path goes unmentioned,
    and nobody reading the blockers learns the config was never found."""
    checkout, linked = worktree
    import ledger

    (checkout / ledger.LEDGER_DIR / ledger.CONFIG_FILE).unlink()
    monkeypatch.chdir(linked)

    assert land.main(["blockers", "--batch", "b-001"]) == 2
    captured = capsys.readouterr()
    assert "auto_merge is disabled in config" in json.loads(captured.out)["blockers"]
    assert str(checkout / ledger.LEDGER_DIR / ledger.CONFIG_FILE) in captured.err


# --- issue #81: the command page documents what `checks` actually returns ------

ROOT = Path(__file__).resolve().parents[1]


def test_the_land_command_documents_every_bucket_and_the_sha_flag():
    """`commands/land.md` is what the operator reads when a gate says `pending`.
    The `stale` bucket and `--sha` went into the skill module and never reached
    the page, so a check parked in `stale` had no documented explanation where
    the command told the reader to look."""
    doc = (ROOT / "commands" / "land.md").read_text()
    for bucket in land.classify_checks([], {}):
        assert f"`{bucket}`" in doc, f"`checks` returns a {bucket} bucket; land.md never names it"
    for term in ("--sha", "head_sha", "reason"):
        assert term in doc, f"land.md never mentions {term}"


# --- review: an unresolvable head SHA must not fall back to the unscoped read -


def _profile_file(tmp_path):
    path = tmp_path / "ci-profile.json"
    path.write_text(json.dumps(PROFILE))
    return str(path)


def test_a_head_sha_that_cannot_be_resolved_reports_pending_not_a_stale_green(
    monkeypatch, capsys, tmp_path
):
    """`gh pr view` can fail transiently, or be too old to know `headRefOid`.

    The check list still answers, and in the minutes after a push it answers with
    the PREVIOUS commit's greens. Falling back to it here is the exact hazard the
    SHA scoping exists to close.
    """
    calls = []

    def fake_gh(args):
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return None
        return [check(n, "SUCCESS") for n in ("lint", "unit", "integration")]

    monkeypatch.setattr(land, "_gh_json", fake_gh)
    code = land.main(["checks", "--pr", "7", "--repo", "o/r", "--profile", _profile_file(tmp_path)])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["head_sha"] is None
    assert out["gate"] == "pending", "an unprovable gate is pending, never green"
    assert out["reason"], "the caller acts on `gate`, so the reason must travel with it"
    assert not any(a[:2] == ["pr", "checks"] for a in calls), (
        f"the unscoped read must never happen without a SHA: {calls}"
    )


def test_a_pr_read_that_answers_without_a_head_sha_is_also_pending(monkeypatch, capsys, tmp_path):
    """An older `gh` drops the unknown field rather than failing the whole call."""
    monkeypatch.setattr(
        land,
        "_gh_json",
        lambda args: (
            {"number": 7, "baseRefName": "main", "labels": []}
            if args[:2] == ["pr", "view"]
            else [check(n, "SUCCESS") for n in ("lint", "unit", "integration")]
        ),
    )
    land.main(["checks", "--pr", "7", "--repo", "o/r", "--profile", _profile_file(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert out["gate"] == "pending" and out["head_sha"] is None
    assert out["base_branch"] == "main", "what the read did learn is still reported"


def test_an_explicit_sha_still_gates_when_the_pr_read_fails(monkeypatch, capsys, tmp_path):
    """`--sha` is the ledger's own record of what was pushed; it needs no PR read."""

    def fake_gh(args):
        if args[:2] == ["pr", "view"]:
            return None
        if "check-runs" in args[-1]:
            return {
                "check_runs": [
                    {"name": n, "status": "completed", "conclusion": "success", "head_sha": NEW}
                    for n in ("lint", "unit", "integration")
                ]
            }
        return {"statuses": []}

    monkeypatch.setattr(land, "_gh_json", fake_gh)
    land.main(
        ["checks", "--pr", "7", "--repo", "o/r", "--sha", NEW, "--profile", _profile_file(tmp_path)]
    )
    out = json.loads(capsys.readouterr().out)
    assert out["head_sha"] == NEW and out["gate"] == "full_green"


# --- review: dropping a stale result must not leave a green in its place ------

UNREQUIRED = {
    "required_checks": [],
    "protection_known": True,
    "jobs": {"lint": job(required=True)},
}


def test_a_stale_failure_leaves_nothing_to_call_green(monkeypatch):
    """Protection naming no context still does not make an empty picture green.

    Dropping the previous commit's red is right — it describes another commit.
    But before the SHA scoping this read `failed`, and reading it `full_green`
    moves the gate in the one direction that costs a merge.
    """
    stale = [sha_check("lint", "FAILURE", OLD)]
    assert land.ci_gate(stale, UNREQUIRED, None, NEW) == "pending"


def test_a_commit_whose_checks_have_not_arrived_is_not_green_either():
    """The SHA-addressed read returns [] in the window right after a push."""
    assert land.ci_gate([], UNREQUIRED, None, NEW) == "pending"


def test_a_repo_that_declares_no_ci_at_all_is_not_held_pending_forever():
    """The boundary: nothing declared and nothing dropped means nothing to wait for."""
    no_ci = {"required_checks": [], "protection_known": True, "jobs": {}}
    assert land.ci_gate([], no_ci, None, NEW) == "full_green"


def test_a_green_for_this_commit_is_still_green_beside_a_dropped_stale_red():
    """Waiting is for what has not reported, not for what has."""
    mixed = [sha_check("lint", "FAILURE", OLD), sha_check("lint", "SUCCESS", NEW)]
    assert land.ci_gate(mixed, UNREQUIRED, None, NEW) == "full_green"


# --- review: an emptiness test measured over the wrong checks -----------------

TWO_JOBS = {
    "required_checks": [],
    "protection_known": True,
    "jobs": {"lint": job(required=True), "test": job(required=True)},
}


def test_a_status_from_no_declared_job_does_not_end_the_wait():
    """The hole the emptiness test left: the list is not empty, the suite is.

    A DCO or CLA status, or a preview deploy, posts within a second of the push.
    That leaves one check in the scoped list, so "nothing describes this commit"
    is false and the guard never fires — while every job the profile declares is
    still missing. Protection naming no required context means nothing can
    *block* a merge; it still does not mean CI has spoken about this commit.
    """
    checks = [sha_check("lint", "FAILURE", OLD), sha_check("DCO", "SUCCESS", NEW)]
    assert land.ci_gate(checks, TWO_JOBS, None, NEW) == "pending"


def test_the_two_protection_branches_read_an_undeclared_status_alike():
    """Whether protection could be read changes nothing about what a bot proves."""
    checks = [sha_check("lint", "FAILURE", OLD), sha_check("DCO", "SUCCESS", NEW)]
    unknown = {**TWO_JOBS, "protection_known": False}
    assert land.ci_gate(checks, unknown, None, NEW) == "pending"
    assert land.ci_gate(checks, TWO_JOBS, None, NEW) == "pending"


def test_a_declared_job_reporting_is_what_ends_the_wait():
    """The other half: waiting stops on the declared suite, not on the bot."""
    checks = [sha_check("DCO", "SUCCESS", NEW), sha_check("lint", "SUCCESS", NEW)]
    assert land.ci_gate(checks, UNREQUIRED, None, NEW) == "full_green"


# --- review: a convergence counter is not a merge ceiling ---------------------

PROGRESS_CAPS = {"pushes": 3, "review_rounds": 2, "reruns": 2, "build_resumes": 3}


def progressed(**counters):
    """A green, cleanly reviewed batch that took some number of attempts to get there."""
    base = {"pushes": 1, "review_rounds": 0, "reruns": 0, "futile_pushes": 0, "build_resumes": 0}
    return ready_batch(attempts={**base, **counters})


def test_a_build_that_converged_is_not_blocked_by_the_resumes_it_took():
    """`caps.build_resumes` is `stalled_build`'s ceiling, and that rule already ended.

    `stalled_build` reads the count only while the batch is still `building`, so
    a build interrupted three times that then reached `built` has converged.
    Nothing resets the counter — by design, the record of what happened stays —
    so `merge_blockers` reading the same key held a fully green, cleanly
    reviewed batch for the rest of its life, and requeueing could not clear it.
    """
    batch = progressed(build_resumes=3)
    assert ledger.stalled_build(batch, PROGRESS_CAPS) is None
    assert ledger.cap_breached(batch, PROGRESS_CAPS) is None
    assert land.merge_blockers(batch, {"labels": []}, {**CFG, "caps": PROGRESS_CAPS}) == []


def test_a_run_of_futile_pushes_is_owned_by_its_own_rule_as_well():
    """The other progress counter, excluded for the same reason rather than by luck.

    `futile_push_run` carries a default ceiling and escalates on its own; it is
    only invisible in `merge_blockers` today because any green resets the run to
    zero. Both counters answer "is this batch converging?", which is a question
    about escalation, not a ceiling on how much may have happened.
    """
    caps = {**PROGRESS_CAPS, "futile_pushes": 3}
    batch = progressed(futile_pushes=3)
    assert ledger.futile_push_run(batch, caps) is not None
    assert land.merge_blockers(batch, {"labels": []}, {**CFG, "caps": caps}) == []


def test_a_runaway_ceiling_still_blocks_beside_a_progress_counter():
    """The exclusion is by counter, not a blanket amnesty on caps."""
    batch = progressed(pushes=3, build_resumes=3)
    blockers = land.merge_blockers(batch, {"labels": []}, {**CFG, "caps": PROGRESS_CAPS})
    assert blockers == ["pushes at cap (3/3)"]


# --- review: mutual containment dropped genuine rewordings -------------------


def test_a_rewording_that_both_adds_and_drops_words_is_still_a_repeat():
    """The shape the containment rule lost: neither summary contains the other.

    One objection, stated twice. Requiring one word set to contain the other read
    it as two rounds of progress and let the review run to the hard ceiling.
    """
    a = finding("scripts/fetch.py", "high", "unbounded retry loop in the fetch helper")
    b = finding("scripts/fetch.py", "high", "the retry loop in fetch has no ceiling")
    assert land.same_finding(a, b) is True
    assert land.review_stalled([[a], [b]], hard_ceiling=5) is not None


def test_two_terms_swapped_in_place_are_still_two_defects():
    """#29 stays fixed, and holds for more than a single swapped word."""
    a = finding("scripts/parse.py", "high", "missing null check in parse_config header")
    b = finding("scripts/parse.py", "high", "missing type check in parse_config footer")
    assert land.same_finding(a, b) is False


def test_the_same_words_in_another_order_are_the_same_complaint():
    a = finding("scripts/flush.py", "high", "the flush path races with the queue drain")
    b = finding("scripts/flush.py", "high", "the queue drain races with the flush path")
    assert land.same_finding(a, b) is True


@pytest.mark.parametrize(
    "left,right",
    [
        ("unbounded retry loop in the fetch helper", "the retry loop in fetch has no ceiling"),
        ("missing null check in parse_config", "missing type check in parse_config"),
        ("connection pool leaks handles on timeout", "connection pool leaks handles on shutdown"),
        ("the retry loop has no ceiling", "retry loop has no upper ceiling at all"),
    ],
)
def test_which_finding_came_first_never_changes_the_answer(left, right):
    """Rounds are compared in one direction, but a rule that is not symmetric is
    a rule nobody can reason about."""
    a = finding("scripts/x.py", "high", left)
    b = finding("scripts/x.py", "high", right)
    assert land.same_finding(a, b) is land.same_finding(b, a)


# --- issue #79: a synonym or a plural swapped in place is not a new defect -----


def test_a_synonym_swapped_in_place_is_the_same_finding():
    """Same shape as `null` -> `type` — one word exchanged, nothing added or
    dropped — and only the words tell the two apart. `review_stalled` therefore
    missed the round-2 repeat, and the locus rule caught it a round later."""
    a = finding("scripts/fetch.py", "high", "unbounded retry loop in fetch_data")
    b = finding("scripts/fetch.py", "high", "unlimited retry loop in fetch_data")
    assert land.same_finding(a, b) is True
    assert land.review_stalled([[a], [b]], hard_ceiling=5) is not None


def test_a_plural_swapped_in_place_is_the_same_finding():
    a = finding("scripts/fetch.py", "high", "unbounded retry loops in fetch_data")
    b = finding("scripts/fetch.py", "high", "unbounded retry loop in fetch_data")
    assert land.same_finding(a, b) is True


def test_synonymy_does_not_reach_the_term_that_carries_the_defect():
    """`missing` and `absent` are one word; `null` and `bounds` are not, and must
    never be, or #29 comes back."""
    a = finding("scripts/parse.py", "high", "missing null check in parse_config")
    b = finding("scripts/parse.py", "high", "absent bounds check in parse_config")
    assert land.same_finding(a, b) is False


@pytest.mark.parametrize(
    "word,expected",
    [("loops", "loop"), ("retries", "retry"), ("checks", "check"), ("boxes", "box")],
)
def test_plurals_fold_to_their_singular(word, expected):
    assert land._stem(word) == expected


@pytest.mark.parametrize("word", ["class", "process", "status", "is", "as"])
def test_words_that_merely_end_in_s_are_not_mangled_into_something_else(word):
    """Stemming `status` to `statu` is harmless only because both sides get the
    same treatment; the rule must not turn a word into a different real word."""
    assert land._stem(word) in {word, word[:-1]}


# --- issue #76: a path list nobody checked against the diff blocks, not clears --


def test_paths_never_confirmed_against_the_diff_block_the_merge():
    """The reported hole: an issue whose prose named no file yields `paths: []`,
    the protected-path loop finds nothing in `[]`, and a fix that edits a
    protected workflow file merges itself. Absence of evidence is not safety."""
    blockers = land.merge_blockers(ready_batch(paths=[], paths_head=None), {"labels": []}, CFG)
    assert len(blockers) == 1
    assert "never confirmed" in blockers[0] and "batch.py paths" in blockers[0]


def test_paths_confirmed_against_the_commit_being_merged_do_not_block():
    pr = {"labels": [], "headRefOid": CONFIRMED}
    assert land.merge_blockers(ready_batch(), pr, CFG) == []


def test_paths_confirmed_against_an_earlier_commit_block_the_merge():
    """A path list is a statement about one commit, like a gate verdict. One
    confirmed before a later push describes code that no longer exists."""
    pr = {"labels": [], "headRefOid": "d" * 40}
    blockers = land.merge_blockers(ready_batch(), pr, CFG)
    assert len(blockers) == 1
    assert "ccccccc" in blockers[0] and "ddddddd" in blockers[0]


def test_the_ledger_head_is_the_fallback_when_the_pr_head_is_unknown():
    """`loop.next_action` and a `blockers` call without --pr have no PR read to
    hand; the last `batch.pushed` the ledger recorded is what is left."""
    batch = ready_batch(head_sha="d" * 40)
    assert land.merge_blockers(batch, {"labels": []}, CFG) != []
    assert land.merge_blockers(ready_batch(head_sha=CONFIRMED), {"labels": []}, CFG) == []


def test_an_abbreviated_head_still_names_the_same_commit():
    pr = {"labels": [], "headRefOid": CONFIRMED[:7]}
    assert land.merge_blockers(ready_batch(), pr, CFG) == []


def test_a_confirmed_protected_path_still_blocks():
    """Confirmation says the list is real; it does not say the list is safe."""
    batch = ready_batch(paths=["src/auth/session.py"])
    blockers = land.merge_blockers(batch, {"labels": [], "headRefOid": CONFIRMED}, CFG)
    assert blockers == ["src/auth/session.py is protected by **/auth/**; never auto-merged"]


def test_the_loop_may_treat_confirming_the_paths_as_part_of_merging():
    """The recipe the loop dispatches on `merge` confirms the paths before it
    asks `blockers`, so for that caller an unconfirmed list is work, not a
    blocker. The CLI never passes this."""
    batch = ready_batch(paths_head=None)
    assert land.merge_blockers(batch, {"labels": []}, CFG) != []
    assert land.merge_blockers(batch, {"labels": []}, CFG, observed_paths_required=False) == []


# --- the revert check is a command, not a stash ------------------------------
# By the time a review runs the fix is committed. `git stash push -- <file>` on
# a clean tree saves nothing and reports success, so the covering test kept
# passing against the very fix it was meant to lose.


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@pytest.fixture
def fixed(tmp_path):
    """A repo whose branch fixes a bug, adds the test that catches it, adds a
    module, and adds a test that would pass whatever the code did."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _write(root, "conftest.py", "")
    _write(root, "src/__init__.py", "")
    _write(root, "src/calc.py", "def add(a, b):\n    return a - b\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base: add subtracts")

    _git(root, "checkout", "-q", "-b", "foreman/b-001")
    _write(root, "src/calc.py", "def add(a, b):\n    return a + b\n")
    _write(root, "src/extra.py", "def twice(x):\n    return 2 * x\n")
    _write(
        root,
        "tests/test_calc.py",
        "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
    )
    _write(
        root,
        "tests/test_extra.py",
        "from src.extra import twice\n\n\ndef test_twice():\n    assert twice(2) == 4\n",
    )
    _write(root, "tests/test_tautology.py", "def test_nothing():\n    assert True\n")
    _write(
        root,
        "tests/test_broken.py",
        "from src.calc import add\n\n\ndef test_wrong():\n    assert add(1, 1) == 3\n",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fix: add adds")
    return root


def _porcelain(root):
    return subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True
    ).stdout


def test_a_test_that_catches_the_fix_fails_once_the_fix_is_reverted(fixed):
    result = land.revert_check(fixed, "main", ["src/calc.py"], ["tests/test_calc.py::test_add"])
    assert result["revert_check"] == "failed_as_expected", result
    assert result["reverted"] == ["src/calc.py"]


def test_a_test_that_passes_either_way_guards_nothing(fixed):
    result = land.revert_check(fixed, "main", ["src/calc.py"], ["tests/test_tautology.py"])
    assert result["revert_check"] == "still_passed"


def test_a_file_the_branch_added_is_removed_to_revert_it(fixed):
    """`git checkout main -- src/extra.py` cannot restore what main never had."""
    result = land.revert_check(fixed, "main", ["src/extra.py"], ["tests/test_extra.py"])
    assert result["revert_check"] == "failed_as_expected"
    assert result["removed"] == ["src/extra.py"]


def test_the_builders_checkout_is_never_touched_and_nothing_is_left_behind(fixed):
    land.revert_check(fixed, "main", ["src/calc.py", "src/extra.py"], ["tests/test_calc.py"])
    assert (fixed / "src" / "calc.py").read_text().endswith("a + b\n")
    assert (fixed / "src" / "extra.py").exists()
    assert _porcelain(fixed) == ""
    listed = subprocess.run(
        ["git", "-C", str(fixed), "worktree", "list"], capture_output=True, text=True
    ).stdout
    assert len(listed.splitlines()) == 1, "the scratch worktree must be removed"


def test_tests_that_fail_with_the_fix_in_place_measure_nothing(fixed):
    """A failure after the revert is only evidence if there was a pass before it."""
    result = land.revert_check(fixed, "main", ["src/calc.py"], ["tests/test_broken.py"])
    assert result["revert_check"] == "not_run"
    assert "with the fix" in result["reason"]


def test_a_source_file_that_exists_nowhere_is_a_reason_not_a_verdict(fixed):
    result = land.revert_check(fixed, "main", ["src/imaginary.py"], ["tests/test_calc.py"])
    assert result["revert_check"] == "not_run"
    assert "imaginary" in result["reason"]


def test_nothing_named_is_not_a_pass(fixed):
    assert land.revert_check(fixed, "main", [], ["tests/test_calc.py"])["revert_check"] == "not_run"
    assert land.revert_check(fixed, "main", ["src/calc.py"], [])["revert_check"] == "not_run"


def test_the_cli_exits_zero_only_for_failed_as_expected(fixed, capsys):
    base = ["revert-check", "--base", "main", "--repo-dir", str(fixed), "--source", "src/calc.py"]
    assert land.main([*base, "--test", "tests/test_calc.py::test_add"]) == 0
    assert json.loads(capsys.readouterr().out)["revert_check"] == "failed_as_expected"
    assert land.main([*base, "--test", "tests/test_tautology.py"]) == 1
    assert json.loads(capsys.readouterr().out)["revert_check"] == "still_passed"


def test_the_recipes_run_the_command_rather_than_stashing():
    root = Path(__file__).resolve().parents[1]
    for doc in ("agents/reviewer.md", "skills/pr-landing/modules/review-gate.md"):
        text = (root / doc).read_text()
        assert 'land.py" revert-check' in text, doc
        assert "git stash push --" not in text.split("```bash")[1].split("```")[0] or (
            "revert-check" in text.split("```bash")[1]
        ), f"{doc} still tells the reviewer to stash"
