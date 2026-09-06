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

# (shape) -> (spec, requirable with no base known, requirable with base "main")
# Production ALWAYS supplies a base branch, so a table that only tests the
# unknown-base column tests the configuration nobody runs.
REQUIRABLE = {
    "plain pull_request": (spec({"pull_request": {}}), True, True),
    "plain push": (spec({"push": {}}), True, True),
    "pull_request and push": (spec({"pull_request": {}, "push": {}}), True, True),
    "pull_request_target": (spec({"pull_request_target": {}}), True, True),
    # branches: on a PR trigger matches the BASE, so a matching base makes it
    # unconditional. On a push trigger it matches the HEAD, which is the PR's
    # feature branch, so it stays conditional whatever the base is.
    "pull_request + branches": (spec({"pull_request": {"branches": ["main"]}}), False, True),
    "push + branches": (spec({"push": {"branches": ["main"]}}), False, False),
    "push branches + plain PR": (
        spec({"push": {"branches": ["main"]}, "pull_request": {}}),
        True,
        True,
    ),
    "pull_request + paths": (spec({"pull_request": {"paths": ["src/**"]}}), False, False),
    "pull_request + pathsignore": (
        spec({"pull_request": {"paths_ignore": ["**.md"]}}),
        False,
        False,
    ),
    "pull_request + branchesignore": (
        spec({"pull_request": {"branches_ignore": ["main"]}}),
        False,
        False,
    ),
    "pull_request + negated branch": (
        spec({"pull_request": {"branches": ["**", "!main"]}}),
        False,
        False,
    ),
    "push + tags": (spec({"push": {"tags": ["v*"]}}), False, False),
    "push + paths": (spec({"push": {"paths": ["src/**"]}}), False, False),
    "schedule only": (spec({"schedule": {}}), False, False),
    "workflow_dispatch only": (spec({"workflow_dispatch": {}}), False, False),
    "workflow_call only": (spec({"workflow_call": {}}), False, False),
    "release only": (spec({"release": {}}), False, False),
    "merge_group only": (spec({"merge_group": {}}), False, False),
    "templated job name": (
        spec({"pull_request": {}}, display="E2E ${{ matrix.os }}"),
        False,
        False,
    ),
    "no triggers at all": (spec({}), False, False),
    "pull_request + open types": (
        spec({"pull_request": {"types": ["opened", "synchronize"]}}),
        True,
        True,
    ),
    "pull_request + closed only": (spec({"pull_request": {"types": ["closed"]}}), False, False),
    "pull_request + labeled only": (spec({"pull_request": {"types": ["labeled"]}}), False, False),
}


@pytest.mark.parametrize("base", [None, "main"], ids=["no_base", "base_main"])
@pytest.mark.parametrize("shape", sorted(REQUIRABLE), ids=lambda s: s.replace(" ", "_"))
def test_requirability_of_every_job_shape(shape, base):
    job_spec, without_base, with_base = REQUIRABLE[shape]
    expected = with_base if base else without_base
    assert land.can_report_on_pr(job_spec, base_branch=base) is expected, f"{shape} base={base}"


@pytest.mark.parametrize(
    "patterns,base,expected",
    [
        (["releases/**"], "releases/1-alpha", True),
        (["releases/**", "!releases/**-alpha"], "releases/1-alpha", False),
        (["**", "!main"], "main", False),
        (["**", "!main"], "develop", True),
        (["release/*"], "release/1/2", False),  # * must not cross a slash
        (["release/**"], "release/1/2", True),
    ],
)
def test_github_branch_filter_semantics(patterns, base, expected):
    """`!` excludes, and `*` does not cross `/`. fnmatch does neither."""
    job = spec({"pull_request": {"branches": patterns}})
    assert land.can_report_on_pr(job, base_branch=base) is expected


# `branches:` on a pull_request trigger matches the PR's BASE branch. When the
# base is known and matches, the job is unconditional in practice — and this is
# the single most common CI shape there is.


@pytest.mark.parametrize(
    "base,expected",
    [
        ("main", True),  # the filter matches: this job always runs on such PRs
        ("release/1.x", False),  # it does not match: the job will not report
        (None, False),  # base unknown: stay conservative
    ],
)
def test_a_branches_filter_is_resolved_against_the_pull_request_base(base, expected):
    assert land.can_report_on_pr(BRANCHED, base_branch=base) is expected


def test_a_wildcard_branches_filter_matches_the_base():
    spec_ = spec({"pull_request": {"branches": ["release/*"]}})
    assert land.can_report_on_pr(spec_, base_branch="release/1.x") is True


def test_a_paths_filter_stays_conditional_even_when_branches_match():
    spec_ = spec({"pull_request": {"branches": ["main"], "paths": ["src/**"]}})
    assert land.can_report_on_pr(spec_, base_branch="main") is False


# --- question 2: the gate, given one requirable job plus one other ----------

UNPROTECTED = {"required_checks": [], "protection_known": False}


def profile(**jobs):
    return {**UNPROTECTED, "jobs": jobs}


def check(name, state):
    return {"name": name, "state": state}


PLAIN = spec({"pull_request": {}})
# `branches:` on a pull_request trigger matches the PR's BASE branch, so for
# every PR foreman opens it matches. Excluding these jobs removed a repo's whole
# CI from the gate, which is how round 6's high finding arose.
BRANCHED = spec({"pull_request": {"branches": ["main"]}})
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
    # An unrelated third-party check is not evidence that this repo's CI ran.
    "unrelated check only": (
        profile(lint=BRANCHED, test=BRANCHED),
        [check("DCO", "SUCCESS")],
        "pending",
    ),
    "unrelated plus profiled": (
        profile(lint=BRANCHED, test=BRANCHED),
        [check("DCO", "SUCCESS"), check("lint", "SUCCESS"), check("test", "SUCCESS")],
        "full_green",
    ),
    "unrelated plus one profiled pending": (
        profile(lint=BRANCHED, test=BRANCHED),
        [check("DCO", "SUCCESS"), check("lint", "PENDING")],
        "pending",
    ),
    # A reusable workflow reports as "caller / called".
    "reusable green": (profile(build=PLAIN), [check("build / compile", "SUCCESS")], "full_green"),
    "reusable pending": (profile(build=PLAIN), [check("build / compile", "PENDING")], "pending"),
    # STALE is terminal: it will never resolve, so it must not read as pending.
    "stale check": (profile(lint=PLAIN), [check("lint", "STALE")], "failed"),
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


@pytest.mark.parametrize("base", [None, "main"], ids=["no_base", "base_main"])
def test_no_cell_hangs_when_every_reportable_job_has_passed(base):
    """The hang direction: a gate that can never clear."""
    for name, (job_spec, without_base, with_base) in REQUIRABLE.items():
        if with_base if base else without_base:
            continue
        prof = profile(lint=PLAIN, other=job_spec)
        assert land.ci_gate([check("lint", "SUCCESS")], prof, base) == "full_green", (
            f"{name} base={base}"
        )
