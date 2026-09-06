"""Batching: group issues so one slow suite run covers several fixes."""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import batch  # noqa: E402
import ledger  # noqa: E402

CONFIG = {"limits": {"max_batch_issues": 3, "max_batch_weight": 5}, "risk_ceiling": "medium"}


def rec(n, risk="low", size="small", paths=()):
    return {
        "issue": n,
        "verdict": "actionable",
        "risk": risk,
        "size": size,
        "paths": list(paths),
        "title": f"issue {n}",
    }


# --- pairwise compatibility ---------------------------------------------------


def test_two_independent_low_risk_issues_can_share_a_batch():
    assert batch.can_group(rec(1, paths=["src/a.py"]), rec(2, paths=["src/b.py"]), CONFIG)[0]


def test_issues_touching_the_same_file_cannot():
    ok, why = batch.can_group(rec(1, paths=["src/a.py"]), rec(2, paths=["src/a.py"]), CONFIG)
    assert not ok and "src/a.py" in why


def test_a_high_risk_issue_never_shares_a_batch():
    ok, why = batch.can_group(rec(1), rec(2, risk="high"), CONFIG)
    assert not ok and "risk" in why


def test_unknown_paths_are_treated_as_possibly_overlapping():
    """No path information is not evidence of independence."""
    ok, why = batch.can_group(rec(1, paths=[]), rec(2, paths=[]), CONFIG)
    assert not ok and "unknown" in why.lower()


# --- grouping -----------------------------------------------------------------


def test_compatible_issues_are_packed_together():
    groups = batch.group_issues(
        [rec(1, paths=["a.py"]), rec(2, paths=["b.py"]), rec(3, paths=["c.py"])], CONFIG
    )
    assert len(groups) == 1
    assert groups[0]["issues"] == [1, 2, 3]


def test_the_issue_cap_closes_a_batch():
    records = [rec(n, paths=[f"f{n}.py"]) for n in range(1, 6)]
    groups = batch.group_issues(records, CONFIG)
    assert [len(g["issues"]) for g in groups] == [3, 2]


def test_the_weight_cap_closes_a_batch_before_the_issue_cap():
    records = [
        rec(1, size="medium", paths=["a.py"]),
        rec(2, size="medium", paths=["b.py"]),
        rec(3, size="medium", paths=["c.py"]),
    ]
    groups = batch.group_issues(records, CONFIG)  # medium=2, cap 5 -> two then one
    assert [len(g["issues"]) for g in groups] == [2, 1]


def test_a_high_risk_issue_gets_a_batch_of_its_own_rather_than_being_dropped():
    groups = batch.group_issues(
        [rec(1, paths=["a.py"]), rec(2, risk="high", paths=["b.py"])], CONFIG
    )
    solo = [g for g in groups if g["issues"] == [2]]
    assert solo and solo[0]["risk"] == "high"
    assert sum(len(g["issues"]) for g in groups) == 2, "no issue may be silently dropped"


def test_only_actionable_records_are_grouped():
    records = [rec(1, paths=["a.py"]), {**rec(2, paths=["b.py"]), "verdict": "needs-repro"}]
    groups = batch.group_issues(records, CONFIG)
    assert [i for g in groups for i in g["issues"]] == [1]


def test_batch_ids_are_stable_and_ordered():
    groups = batch.group_issues([rec(n, paths=[f"f{n}.py"]) for n in range(1, 5)], CONFIG)
    assert [g["id"] for g in groups] == ["b-001", "b-002"]


def test_grouping_is_deterministic_regardless_of_input_order():
    records = [rec(n, paths=[f"f{n}.py"]) for n in (3, 1, 2)]
    a = batch.group_issues(records, CONFIG)
    b = batch.group_issues(list(reversed(records)), CONFIG)
    assert [g["issues"] for g in a] == [g["issues"] for g in b]


def test_a_batch_carries_the_union_of_its_paths_and_its_worst_risk():
    groups = batch.group_issues(
        [rec(1, paths=["a.py"]), rec(2, risk="medium", paths=["b.py"])], CONFIG
    )
    assert set(groups[0]["paths"]) == {"a.py", "b.py"}
    assert groups[0]["risk"] == "medium"


# --- the economics ------------------------------------------------------------


def test_savings_are_the_suite_runs_not_taken():
    profile = {"cheap_tier_s": 120, "expensive_tier_s": 2280}
    groups = [{"issues": [1, 2, 3]}, {"issues": [4]}]
    saved = batch.estimate_savings(groups, profile)
    assert saved["suite_runs_saved"] == 2  # 4 issues, 2 batches
    assert saved["seconds_saved"] == 2 * 2400
    assert saved["batched_s"] == 2 * 2400 and saved["unbatched_s"] == 4 * 2400


def test_no_savings_claimed_when_every_batch_holds_one_issue():
    saved = batch.estimate_savings(
        [{"issues": [1]}, {"issues": [2]}], {"cheap_tier_s": 60, "expensive_tier_s": 60}
    )
    assert saved["suite_runs_saved"] == 0 and saved["seconds_saved"] == 0


def test_an_unprofiled_repo_reports_savings_it_cannot_quantify():
    saved = batch.estimate_savings([{"issues": [1, 2]}], {})
    assert saved["suite_runs_saved"] == 1
    assert saved["seconds_saved"] is None


# --- splitting a failed batch -------------------------------------------------


def test_a_failing_issue_peels_off_and_the_rest_proceed():
    failing, rest = batch.split(
        {"id": "b-001", "issues": [1, 2, 3], "risk": "low", "paths": ["a.py"]}, failing_issue=2
    )
    assert failing["issues"] == [2] and rest["issues"] == [1, 3]
    assert failing["id"] == "b-001a" and rest["id"] == "b-001b"
    assert failing["split_from"] == "b-001"


def test_splitting_a_single_issue_batch_is_refused():
    with pytest.raises(batch.CannotSplit):
        batch.split({"id": "b-001", "issues": [1]}, failing_issue=1)


def test_splitting_on_an_issue_outside_the_batch_is_refused():
    with pytest.raises(batch.CannotSplit):
        batch.split({"id": "b-001", "issues": [1, 2]}, failing_issue=9)


# --- issue #55: batch ids must not collide with ids already in the ledger -----


def _actionable(*numbers):
    """All on one file, so no two may share a batch and each gets its own id."""
    return [
        {"issue": n, "verdict": "actionable", "size": "small", "risk": "low", "paths": ["same.py"]}
        for n in numbers
    ]


def test_ids_continue_past_the_ones_already_in_the_ledger():
    """Numbering restarted at b-001 on every run, so a second batching run
    reused the id of an already-merged batch."""
    groups = batch.group_issues(_actionable(11, 12), {}, taken={"b-001", "b-002"})
    assert [g["id"] for g in groups] == ["b-003", "b-004"]


def test_numbering_is_unchanged_on_a_fresh_ledger():
    groups = batch.group_issues(_actionable(11, 12), {})
    assert [g["id"] for g in groups] == ["b-001", "b-002"]


def test_a_gap_in_taken_ids_is_not_reused():
    groups = batch.group_issues(_actionable(11), {}, taken={"b-003"})
    assert [g["id"] for g in groups] == ["b-004"]


# --- issue #57: a batch's real paths come from the diff, not from issue prose --


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args], cwd=root, check=True, capture_output=True
    )


@pytest.fixture
def repo(tmp_path):
    """A repo whose branch changes one file the issues named and one they did not."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "foreman@example.com")
    _git(root, "config", "user.name", "foreman")
    (root / "src").mkdir()
    (root / "src" / "upload.py").write_text("x = 1\n")
    (root / "src" / "auth.py").write_text("y = 1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")

    _git(root, "checkout", "-q", "-b", "foreman/b-001")
    (root / "src" / "upload.py").write_text("x = 2\n")
    (root / "src" / "auth.py").write_text("y = 2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "fix")
    return root


def test_the_paths_of_a_branch_are_read_from_the_diff(repo):
    assert batch.diff_paths("main", repo=repo) == ["src/auth.py", "src/upload.py"]


def test_a_file_no_issue_mentioned_is_reported_as_undeclared(repo):
    """The protected-path hole: the merge gate saw intent, never the change."""
    seen = batch.observed_paths({"id": "b-001", "paths": ["src/upload.py"]}, "main", repo=repo)
    assert seen["paths"] == ["src/auth.py", "src/upload.py"]
    assert seen["undeclared"] == ["src/auth.py"]


def test_a_path_the_prose_invented_is_reported_as_untouched(repo):
    """Prose extraction produced scripts/gate.py, which has never existed."""
    seen = batch.observed_paths(
        {"id": "b-001", "paths": ["src/upload.py", "scripts/gate.py"]}, "main", repo=repo
    )
    assert seen["untouched"] == ["scripts/gate.py"]


def test_a_branch_that_changes_nothing_yields_no_paths(repo):
    """git's own answer, reported faithfully. Refusing to act on it is observed_paths' job."""
    _git(repo, "checkout", "-q", "main")
    assert batch.diff_paths("main", repo=repo) == []


def test_an_unreadable_diff_is_raised_not_reported_as_no_paths(repo):
    """An empty path list clears the protected-path gate. git failing must not."""
    with pytest.raises(batch.PathsUnavailable):
        batch.diff_paths("no-such-base", repo=repo)


def test_git_missing_from_path_refuses_the_same_way_a_failed_diff_does(repo, tmp_path):
    """`gate._git` catches OSError for exactly this; the two siblings must agree.

    Only a non-zero exit was converted here, so with no git on PATH the command
    died on a `FileNotFoundError` traceback instead of the one-line refusal.
    """
    empty = tmp_path / "no-tools"
    empty.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", str(empty))
        with pytest.raises(batch.PathsUnavailable):
            batch.diff_paths("main", repo=repo)


def test_the_paths_command_exits_1_when_git_cannot_be_run(repo, tmp_path, capsys):
    """The refusal a caller sees: exit 1 and a reason, not a stack trace."""
    root = ledger.init(tmp_path / "ledger-home")
    ledger.append(root, "batch.created", batch="b-001", issues=[1], paths=["src/upload.py"])
    empty = tmp_path / "no-tools"
    empty.mkdir()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("PATH", str(empty))
        rc = batch.main(
            [
                "paths",
                "--batch",
                "b-001",
                "--base",
                "main",
                "--ledger",
                str(root),
                "--repo-dir",
                str(repo),
            ]
        )
    assert rc == 1
    assert "git" in capsys.readouterr().err


def test_an_empty_diff_is_no_observation_of_what_a_batch_touches(repo):
    """git succeeded and still said nothing, so there is nothing to replace intent with."""
    _git(repo, "checkout", "-q", "main")
    with pytest.raises(batch.PathsUnavailable):
        batch.observed_paths({"id": "b-001", "paths": ["src/auth/session.py"]}, "main", repo=repo)


def test_an_empty_diff_never_clears_the_paths_the_merge_gate_reads(repo, tmp_path, capsys):
    """The reported hole: --apply run from a checkout without the work wrote []."""
    _git(repo, "checkout", "-q", "main")  # the branch is in a linked worktree; this one is clean
    root = ledger.init(tmp_path / "ledger-home")
    ledger.append(root, "batch.created", batch="b-001", issues=[1], paths=["src/auth/session.py"])
    rc = batch.main(
        [
            "paths",
            "--batch",
            "b-001",
            "--base",
            "main",
            "--ledger",
            str(root),
            "--repo-dir",
            str(repo),
            "--apply",
        ]
    )
    assert rc == 1
    assert ledger.load(root).batches["b-001"]["paths"] == ["src/auth/session.py"]
    assert "b-001" in capsys.readouterr().err


def test_recomputed_paths_replace_the_prose_ones_in_the_ledger(repo, tmp_path):
    """The ledger is where land reads paths from, so that is where they must land."""
    root = ledger.init(tmp_path / "ledger-home")
    ledger.append(root, "batch.created", batch="b-001", issues=[1], paths=["src/upload.py"])
    rc = batch.main(
        [
            "paths",
            "--batch",
            "b-001",
            "--base",
            "main",
            "--ledger",
            str(root),
            "--repo-dir",
            str(repo),
            "--apply",
        ]
    )
    assert rc == 0
    assert ledger.load(root).batches["b-001"]["paths"] == ["src/auth.py", "src/upload.py"]


def test_without_apply_the_ledger_is_left_alone(repo, tmp_path, capsys):
    root = ledger.init(tmp_path / "ledger-home")
    ledger.append(root, "batch.created", batch="b-001", issues=[1], paths=["src/upload.py"])
    rc = batch.main(
        [
            "paths",
            "--batch",
            "b-001",
            "--base",
            "main",
            "--ledger",
            str(root),
            "--repo-dir",
            str(repo),
        ]
    )
    reported = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert reported["undeclared"] == ["src/auth.py"]
    assert ledger.load(root).batches["b-001"]["paths"] == ["src/upload.py"]


def test_recomputing_paths_for_a_batch_the_ledger_does_not_know_is_refused(repo, tmp_path):
    root = ledger.init(tmp_path / "ledger-home")
    rc = batch.main(
        [
            "paths",
            "--batch",
            "b-404",
            "--base",
            "main",
            "--ledger",
            str(root),
            "--repo-dir",
            str(repo),
        ]
    )
    assert rc == 1, "a batch.meta event for an unknown batch is silently dropped by the fold"


# --- issue #67: the docstring is the CLI's documentation, so it must be true --


def _documented_flags(subcommand: str) -> set[str]:
    """The flags the module docstring shows for one subcommand, continuations included."""
    lines, collecting = [], False
    for line in batch.__doc__.splitlines():
        stripped = line.strip()
        if stripped.startswith("batch.py "):
            collecting = stripped.startswith(f"batch.py {subcommand} ")
        elif not stripped:
            collecting = False
        if collecting:
            lines.append(stripped)
    assert lines, f"the docstring documents no {subcommand!r} subcommand at all"
    return set(re.findall(r"--[a-z-]+", " ".join(lines)))


def _accepted_flags(subcommand: str) -> set[str]:
    """The flags argparse really accepts. argparse exposes subparsers privately only."""
    parser = batch.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return {
        option
        for action in sub.choices[subcommand]._actions
        for option in action.option_strings
        if option.startswith("--") and option != "--help"
    }


@pytest.mark.parametrize("subcommand", ["plan", "apply", "paths"])
def test_the_docstring_documents_every_flag_a_subcommand_accepts(subcommand):
    """plan reads the ledger to allocate ids. A caller following a docstring that
    omitted --ledger got an empty taken set and ids restarting at b-001."""
    assert _accepted_flags(subcommand) <= _documented_flags(subcommand)


def test_planning_against_a_ledger_that_is_not_there_says_so(tmp_path, capsys, monkeypatch):
    """Silence looks identical to a fresh repo, and the ids collide either way."""
    monkeypatch.chdir(tmp_path)  # not this repository: its own config must not leak in
    triage_file = tmp_path / "triage.json"
    triage_file.write_text(json.dumps({"triaged": _actionable(11)}))
    missing = tmp_path / "elsewhere" / ".foreman"
    rc = batch.main(["plan", "--triage", str(triage_file), "--ledger", str(missing)])
    captured = capsys.readouterr()
    assert rc == 0
    assert str(missing) in captured.err
    assert json.loads(captured.out)["batches"][0]["id"] == "b-001"


# --- issue #74: the config and profile live with the repository ---------------


@pytest.fixture
def worktree(tmp_path):
    """The layout `commands/build.md` prescribes: `.foreman` lives one repo up."""
    checkout = tmp_path / "repo"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "config", "user.email", "foreman@example.com")
    _git(checkout, "config", "user.name", "foreman")
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "root")
    linked = tmp_path / "foreman-b-001"
    _git(checkout, "worktree", "add", "-q", str(linked), "-b", "foreman/b-001")
    root = ledger.init(checkout)
    (root / ledger.CONFIG_FILE).write_text(json.dumps({"limits": {"max_batch_issues": 1}}))
    (root / ledger.PROFILE_FILE).write_text(
        json.dumps({"cheap_tier_s": 100, "expensive_tier_s": 900})
    )
    return checkout, linked


def test_plan_reads_its_config_and_profile_from_the_repository_when_run_from_a_worktree(
    worktree, monkeypatch, capsys, tmp_path
):
    """Read against the caller, a plan cut from a worktree saw no config — so no
    issue cap and no risk ceiling — and no profile, so it could not price what
    its batching saved. And it warned that the ledger was missing, which it was
    not; only the unanchored `.exists()` test thought so."""
    _checkout, linked = worktree
    triage_file = tmp_path / "triage.json"
    triage_file.write_text(
        json.dumps(
            {
                "triaged": [
                    {**_actionable(1)[0], "paths": ["a.py"]},
                    {**_actionable(2)[0], "paths": ["b.py"]},
                ]
            }
        )
    )
    monkeypatch.chdir(linked)

    rc = batch.main(["plan", "--triage", str(triage_file)])
    captured = capsys.readouterr()
    plan = json.loads(captured.out)
    assert rc == 0
    assert [b["issues"] for b in plan["batches"]] == [[1], [2]], "max_batch_issues was read"
    assert plan["savings"]["suite_seconds"] == 1000, "the profile was read"
    assert "warning" not in captured.err, captured.err
