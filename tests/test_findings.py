"""Review findings become GitHub issues, so the loop can pick them up again."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import findings  # noqa: E402

LABELS = ["bug", "enhancement", "documentation", "question", "duplicate"]
CONTEXT = {"batch": "b-001", "pr": 7, "round": 2, "repo": "o/r"}


def f(**kw):
    base = {
        "severity": "high",
        "file": "scripts/triage.py",
        "line": 75,
        "summary": "Security vocabulary omits the bare word auth",
        "failure_scenario": "risk_level('auth bypass on the admin API') returns medium",
    }
    return {**base, **kw}


# --- one finding, one issue ---------------------------------------------------


def test_a_finding_becomes_a_titled_issue():
    issue = findings.to_issue(f(), CONTEXT, LABELS)
    assert issue["title"] == "Security vocabulary omits the bare word auth"
    assert "scripts/triage.py" in issue["body"]
    assert "risk_level" in issue["body"], "the failure scenario must survive"


def test_the_body_records_where_the_finding_came_from():
    body = findings.to_issue(f(), CONTEXT, LABELS)["body"]
    assert "b-001" in body and "#7" in body and "round 2" in body
    assert "high" in body


def test_a_long_summary_is_truncated_into_a_usable_title():
    long = "The retry loop has no ceiling and " + "spins forever on a persistent 503 " * 6
    issue = findings.to_issue(f(summary=long), CONTEXT, LABELS)
    assert len(issue["title"]) <= 100
    assert issue["title"].endswith("…")
    assert long.strip() in issue["body"], "the full summary belongs in the body"


def test_a_finding_with_no_summary_is_refused_rather_than_filed_empty():
    with pytest.raises(findings.UnusableFinding):
        findings.to_issue(f(summary=""), CONTEXT, LABELS)


# --- labels stay inside the repo vocabulary ----------------------------------


@pytest.mark.parametrize(
    "severity,expected",
    [
        ("high", "bug"),
        ("medium", "bug"),
        ("low", "enhancement"),
    ],
)
def test_severity_maps_onto_labels_the_repo_defines(severity, expected):
    assert findings.to_issue(f(severity=severity), CONTEXT, LABELS)["labels"] == [expected]


def test_a_documentation_finding_is_labelled_as_one():
    issue = findings.to_issue(
        f(file="skills/issue-triage/modules/taxonomy.md", severity="low"), CONTEXT, LABELS
    )
    assert issue["labels"] == ["documentation"]


def test_labels_the_repo_does_not_define_are_dropped():
    assert findings.to_issue(f(), CONTEXT, ["question"])["labels"] == []


# --- never file the same finding twice ---------------------------------------

OPEN = [
    {
        "number": 11,
        "title": "Security vocabulary omits bare auth, en-GB spellings, and oauth2",
        "state": "open",
    },
    {"number": 3, "title": "Short issue titles produce false duplicates", "state": "open"},
]


def test_a_finding_already_on_the_tracker_is_not_filed_again():
    plan = findings.plan([f()], CONTEXT, LABELS, OPEN)
    assert plan["file"] == []
    assert plan["skipped"][0]["duplicate_of"] == 11


def test_a_genuinely_new_finding_is_filed():
    new = f(summary="Audit log is corrupted by any multi-line argument", file="scripts/gh_safe.sh")
    plan = findings.plan([new], CONTEXT, LABELS, OPEN)
    assert len(plan["file"]) == 1
    assert plan["skipped"] == []


def test_a_closed_issue_does_not_suppress_a_recurrence():
    closed = [
        {
            "number": 11,
            "title": "Security vocabulary omits bare auth, en-GB spellings",
            "state": "closed",
        }
    ]
    plan = findings.plan([f()], CONTEXT, LABELS, closed)
    assert len(plan["file"]) == 1, "a regression of a closed issue is news"


def test_every_severity_is_filed_including_low():
    """Low findings do not block a merge, which is exactly why they need tracking."""
    plan = findings.plan(
        [f(severity="low", summary="Docstring contradicts the fix")], CONTEXT, LABELS, []
    )
    assert len(plan["file"]) == 1


def test_unusable_findings_are_reported_not_silently_dropped():
    plan = findings.plan([f(), f(summary="")], CONTEXT, LABELS, [])
    assert len(plan["unusable"]) == 1


# --- the verdict as a whole ---------------------------------------------------


def test_a_clean_verdict_yields_nothing_to_file():
    plan = findings.from_verdict({"verdict": "clean", "findings": []}, CONTEXT, LABELS, [])
    assert plan["file"] == []


def test_findings_are_filed_worst_first():
    verdict = {
        "verdict": "changes_requested",
        "findings": [
            f(severity="low", summary="Docstring is stale", file="a.py"),
            f(severity="high", summary="Gate is green on red CI", file="b.py"),
            f(severity="medium", summary="Partial list reads complete", file="c.py"),
        ],
    }
    plan = findings.from_verdict(verdict, CONTEXT, LABELS, [])
    assert [i["severity"] for i in plan["file"]] == ["high", "medium", "low"]


def test_two_short_titles_sharing_one_word_are_not_duplicates():
    """The failure mode issue #3 describes, guarded here from the start."""
    existing = [{"number": 20, "title": "Cache is broken", "state": "open"}]
    plan = findings.plan([f(summary="Parser is broken")], CONTEXT, LABELS, existing)
    assert len(plan["file"]) == 1


def test_a_terser_phrasing_of_a_tracked_issue_is_still_a_duplicate():
    existing = [
        {
            "number": 21,
            "state": "open",
            "title": "The audit log written by gh_safe.sh is corrupted by any "
            "multi-line argument passed to it",
        }
    ]
    plan = findings.plan(
        [f(summary="Audit log corrupted by multi-line argument")], CONTEXT, LABELS, existing
    )
    assert plan["file"] == [] and plan["skipped"][0]["duplicate_of"] == 21
