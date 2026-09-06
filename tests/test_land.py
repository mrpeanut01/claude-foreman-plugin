"""Landing: read CI honestly, judge the reviewer, and refuse to merge on doubt."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import land  # noqa: E402

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


def ready_batch(**kw):
    base = {
        "id": "b-001",
        "state": "ready",
        "ci_gate": "full_green",
        "review_gate": "clean",
        "attempts": {"pushes": 1, "review_rounds": 0, "reruns": 0},
        "paths": ["src/upload.py"],
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
