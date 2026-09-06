"""Review findings become GitHub issues, so the loop can pick them up again."""

import json
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


# --- issue #20: duplicates within a single run -------------------------------


def test_the_same_defect_twice_in_one_verdict_is_filed_once():
    plan = findings.plan([f(), f()], CONTEXT, LABELS, [])
    assert len(plan["file"]) == 1
    assert len(plan["skipped"]) == 1


def test_two_genuinely_different_findings_are_both_filed():
    plan = findings.plan(
        [
            f(file="a.py", summary="Retry loop has no ceiling at all"),
            f(file="b.py", summary="Audit log corrupted by newlines"),
        ],
        CONTEXT,
        LABELS,
        [],
    )
    assert len(plan["file"]) == 2


# --- issue #21: short titles and unusable summaries --------------------------


def test_two_word_titles_can_still_be_recognised_as_duplicates():
    existing = [{"number": 9, "title": "Race condition", "state": "open"}]
    plan = findings.plan([f(summary="Race condition")], CONTEXT, LABELS, existing)
    assert plan["file"] == [] and plan["skipped"][0]["duplicate_of"] == 9


def test_short_titles_sharing_only_one_word_are_still_distinct():
    existing = [{"number": 9, "title": "Cache is broken", "state": "open"}]
    plan = findings.plan([f(summary="Parser is broken")], CONTEXT, LABELS, existing)
    assert len(plan["file"]) == 1


@pytest.mark.parametrize("summary", ["...!?", "   ", "---", "###"])
def test_a_summary_with_no_words_is_refused(summary):
    with pytest.raises(findings.UnusableFinding):
        findings.to_issue(f(summary=summary), CONTEXT, LABELS)


# --- issue #22: worst really means worst -------------------------------------


def test_critical_and_blocker_outrank_high():
    verdict = {
        "findings": [
            f(severity="low", file="a.py", summary="Stale docstring here"),
            f(severity="high", file="b.py", summary="Gate green on red CI"),
            f(severity="blocker", file="c.py", summary="Data loss on retry"),
            f(severity="critical", file="d.py", summary="Secrets in the log"),
            f(severity="medium", file="e.py", summary="Partial list reads full"),
        ]
    }
    plan = findings.from_verdict(verdict, CONTEXT, LABELS, [])
    assert [i["severity"] for i in plan["file"]] == ["critical", "blocker", "high", "medium", "low"]


# --- issue #23: spend the fetch window on rows that are used -----------------


def test_the_tracker_fetch_asks_only_for_open_issues(monkeypatch):
    seen = {}

    def fake(args):
        seen["args"] = args
        return []

    monkeypatch.setattr(findings, "_gh_json", fake)
    findings.fetch_open_issues("o/r")
    assert "--state" in seen["args"]
    assert seen["args"][seen["args"].index("--state") + 1] == "open"


# --- issue #30: a short tracker title must not swallow a longer finding -------


def test_a_two_token_tracker_title_does_not_swallow_a_much_longer_finding():
    """The floor scales down for short titles. It must not scale down for a long one.

    Every token of "Flaky tests" appears in the longer summary, so the overlap
    coefficient is a perfect 1.0 against the shorter title — which is exactly why
    two shared tokens are not enough evidence when the other title has nine.
    """
    existing = [{"number": 9, "title": "Flaky tests", "state": "open"}]
    result = findings.plan(
        [f(summary="Flaky tests in the upload suite mask a real regression in land.py")],
        CONTEXT,
        LABELS,
        existing,
    )
    assert len(result["file"]) == 1, "a distinct finding must not be lost to a tracker stub"
    assert result["skipped"] == []


def test_two_word_titles_are_still_deduped_against_each_other():
    """The relaxed floor still has to do the job it was added for."""
    existing = [{"number": 9, "title": "Race condition", "state": "open"}]
    result = findings.plan([f(summary="Race condition")], CONTEXT, LABELS, existing)
    assert result["file"] == [] and result["skipped"][0]["duplicate_of"] == 9


def test_a_duplicate_of_something_queued_this_run_claims_no_issue_number():
    """Nothing has been filed yet, so there is no number a caller could follow."""
    result = findings.plan([f(), f()], CONTEXT, LABELS, [])
    skipped = result["skipped"][0]
    assert skipped["duplicate_of"] is None
    assert skipped["duplicate_of_title"] == result["file"][0]["title"]


def test_already_tracked_holds_issue_numbers_and_nothing_else(tmp_path, monkeypatch, capsys):
    """The reported symptom: 'pending:Audit log corrupted…' where a number belongs."""
    monkeypatch.setattr(findings, "create_issue", lambda repo, issue, wrapper: "https://x/99")
    repeated = f(summary="Audit log is corrupted by any multi-line argument", file="gh_safe.sh")
    result = findings.plan([f(), repeated, repeated], CONTEXT, LABELS, OPEN)

    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps({**result, "context": CONTEXT}))
    rc = findings.main(
        ["file", "--plan", str(plan_file), "--repo", "o/r", "--ledger", str(tmp_path)]
    )
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["already_tracked"] == [11]
    assert all(isinstance(n, int) for n in out["already_tracked"])
    assert out["duplicate_within_run"] == [repeated["summary"]]


# --- issue #61: the provenance line must not render a null -------------------


@pytest.mark.parametrize(
    "context", [{"batch": "dogfood-2", "round": 1}, {"batch": "dogfood-2", "pr": None, "round": 1}]
)
def test_a_finding_raised_outside_a_pull_request_names_no_pr(context):
    """--pr is optional, and the provenance line is the only trace back to the
    review that raised the finding, so it is exactly the line that must not
    render a null."""
    first = findings.to_issue(f(), context, LABELS)["body"].splitlines()[0]
    assert "None" not in first and "PR #" not in first
    assert "dogfood-2" in first and "round 1" in first


def test_a_finding_raised_on_a_pull_request_still_names_it():
    first = findings.to_issue(f(), CONTEXT, LABELS)["body"].splitlines()[0]
    assert "PR #7" in first


# --- a plan built for one repository is not filed against another -------------


def test_a_plan_for_another_repository_files_nothing(tmp_path, monkeypatch, capsys):
    """`findings.py file` creates issues, which the wrapper cannot undo, so the
    check has to come before the first one."""

    def never(repo, issue, wrapper):
        raise AssertionError("no issue may be created for a plan built for another repo")

    monkeypatch.setattr(findings, "create_issue", never)
    result = findings.plan([f()], CONTEXT, LABELS, [])
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps({**result, "context": CONTEXT}))  # context.repo is o/r
    rc = findings.main(
        ["file", "--plan", str(plan_file), "--repo", "someone/else", "--ledger", str(tmp_path)]
    )
    assert rc == 1
    assert "o/r" in capsys.readouterr().err
