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

UNPROTECTED = {
    "required_checks": [],
    "protection_known": False,
    "jobs": {
        "lint": {"tier": "cheap", "required": False},
        "test": {"tier": "cheap", "required": False},
    },
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
