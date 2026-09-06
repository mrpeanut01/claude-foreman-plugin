"""The complete truth table for ci_gate's unknown-protection branch.

This branch has been the blocking review finding for four consecutive rounds,
alternating between merging too early and hanging forever. Each round fixed the
cell that was named and broke a cell that was not. So the cells are enumerated
here first, as a spec, and the implementation is written to satisfy them.

Two questions decide every cell:

  1. Can this job produce a check on this PR at all?  -> requirable
  2. What is the check saying right now?              -> covered / pending / failed

A job is requirable only when some trigger will fire unconditionally on a pull
request. Any filter — paths, paths-ignore, branches, tags — makes it
conditional, and a conditional job counts only once it actually reports.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import land  # noqa: E402


def spec(events, display=None):
    return {
        "events": events,
        "display": display,
        "tier": "cheap",
        "triggers": sorted(events),
        "path_filters": [],
        "pr_path_filters": [],
    }


# --- question 1: can this job produce a check on this pull request? ----------

REQUIRABLE = {
    "plain pull_request": (spec({"pull_request": {}}), True),
    "plain push": (spec({"push": {}}), True),
    "pull_request and push": (spec({"pull_request": {}, "push": {}}), True),
    "pull_request_target": (spec({"pull_request_target": {}}), True),
    "pull_request + paths": (spec({"pull_request": {"paths": ["src/**"]}}), False),
    "pull_request + pathsignore": (spec({"pull_request": {"paths_ignore": ["**.md"]}}), False),
    "pull_request + branches": (spec({"pull_request": {"branches": ["main"]}}), False),
    "push + branches": (spec({"push": {"branches": ["main"]}}), False),
    "push + tags": (spec({"push": {"tags": ["v*"]}}), False),
    "push + paths": (spec({"push": {"paths": ["src/**"]}}), False),
    "schedule only": (spec({"schedule": {}}), False),
    "workflow_dispatch only": (spec({"workflow_dispatch": {}}), False),
    "workflow_call only": (spec({"workflow_call": {}}), False),
    "release only": (spec({"release": {}}), False),
    "templated job name": (spec({"pull_request": {}}, display="E2E ${{ matrix.os }}"), False),
    "no triggers at all": (spec({}), False),
    # A restricted trigger alongside an unrestricted one still reports.
    "push branches + plain PR": (spec({"push": {"branches": ["main"]}, "pull_request": {}}), True),
}


@pytest.mark.parametrize("shape", sorted(REQUIRABLE), ids=lambda s: s.replace(" ", "_"))
def test_requirability_of_every_job_shape(shape):
    job_spec, expected = REQUIRABLE[shape]
    assert land.can_report_on_pr(job_spec) is expected, shape


# --- question 2: the gate, given one requirable job plus one other ----------

UNPROTECTED = {"required_checks": [], "protection_known": False}


def profile(**jobs):
    return {**UNPROTECTED, "jobs": jobs}


def check(name, state):
    return {"name": name, "state": state}


PLAIN = spec({"pull_request": {}})
CONDITIONAL = spec({"pull_request": {"paths": ["src/**"]}})
UNREPORTABLE = spec({"schedule": {}})

GATE = {
    # a requirable job that has not reported holds the gate
    "requirable absent": (profile(lint=PLAIN, test=PLAIN), [check("lint", "SUCCESS")], "pending"),
    "requirable pending": (profile(lint=PLAIN), [check("lint", "PENDING")], "pending"),
    "requirable success": (profile(lint=PLAIN), [check("lint", "SUCCESS")], "full_green"),
    "requirable failure": (profile(lint=PLAIN), [check("lint", "FAILURE")], "failed"),
    "requirable cancelled": (profile(lint=PLAIN), [check("lint", "CANCELLED")], "failed"),
    # a conditional job counts only once it reports — but then it counts fully
    "conditional absent": (
        profile(lint=PLAIN, docs=CONDITIONAL),
        [check("lint", "SUCCESS")],
        "full_green",
    ),
    "conditional pending": (
        profile(lint=PLAIN, docs=CONDITIONAL),
        [check("lint", "SUCCESS"), check("docs", "PENDING")],
        "pending",
    ),
    "conditional failure": (
        profile(lint=PLAIN, docs=CONDITIONAL),
        [check("lint", "SUCCESS"), check("docs", "FAILURE")],
        "failed",
    ),
    # a job that can never report must never hold the gate
    "unreportable absent": (
        profile(lint=PLAIN, nightly=UNREPORTABLE),
        [check("lint", "SUCCESS")],
        "full_green",
    ),
    # nothing requirable and nothing reported is not evidence of green
    "no requirable, no checks": (profile(docs=CONDITIONAL), [], "pending"),
    "no requirable, one green": (
        profile(docs=CONDITIONAL),
        [check("docs", "SUCCESS")],
        "full_green",
    ),
    "no requirable, one pending": (
        profile(docs=CONDITIONAL),
        [check("docs", "PENDING")],
        "pending",
    ),
    "no jobs at all, no checks": (profile(), [], "pending"),
    # matrix cells satisfy the job that declared them
    "matrix all green": (
        profile(lint=PLAIN, test=PLAIN),
        [
            check("lint", "SUCCESS"),
            check("test (3.11)", "SUCCESS"),
            check("test (3.12)", "SUCCESS"),
        ],
        "full_green",
    ),
    "matrix one pending": (
        profile(lint=PLAIN, test=PLAIN),
        [
            check("lint", "SUCCESS"),
            check("test (3.11)", "SUCCESS"),
            check("test (3.12)", "PENDING"),
        ],
        "pending",
    ),
}


@pytest.mark.parametrize("cell", sorted(GATE), ids=lambda s: s.replace(" ", "_").replace(",", ""))
def test_the_gate_for_every_cell(cell):
    prof, checks, expected = GATE[cell]
    assert land.ci_gate(checks, prof) == expected, cell


# --- the two directions this branch has failed in, stated as invariants ------


def test_no_cell_returns_green_while_a_check_is_running():
    for cell, (prof, checks, expected) in GATE.items():
        if any(c["state"] == "PENDING" for c in checks):
            assert expected != "full_green", f"{cell} would merge with a check in flight"
            assert land.ci_gate(checks, prof) != "full_green", cell


def test_no_cell_hangs_when_every_reportable_job_has_passed():
    """The hang direction: a gate that can never clear."""
    for name, (job_spec, requirable) in REQUIRABLE.items():
        if requirable:
            continue
        prof = profile(lint=PLAIN, other=job_spec)
        assert land.ci_gate([check("lint", "SUCCESS")], prof) == "full_green", name
