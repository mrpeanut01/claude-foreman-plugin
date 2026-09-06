"""Status digest: what is in flight, what is stuck, and what needs a human."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ledger  # noqa: E402
import status  # noqa: E402


@pytest.fixture
def root(tmp_path):
    return ledger.init(tmp_path)


def test_empty_ledger_says_so(root):
    out = status.render(ledger.fold(ledger.read_events(root)), config={})
    assert "no batches" in out.lower()


def test_in_flight_batch_shows_state_and_gates(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[42, 43], pr=7)
    for s in ("building", "built", "open"):
        ledger.transition(root, "b-001", s)
    ledger.gate(root, "b-001", "ci", "cheap_green")
    out = status.render(ledger.fold(ledger.read_events(root)), config={})
    assert "b-001" in out and "open" in out
    assert "cheap_green" in out and "pending" in out
    assert "#42" in out and "#43" in out


def test_escalation_is_shown_with_its_reason(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    ledger.transition(root, "b-001", "building")
    ledger.transition(root, "b-001", "escalated")
    ledger.append(root, "escalation", batch="b-001", reason="same test failed 3 pushes running")
    out = status.render(ledger.fold(ledger.read_events(root)), config={})
    assert "same test failed 3 pushes running" in out


def test_batch_at_its_cap_is_flagged_as_needing_a_human(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    for s in ("building", "built", "open"):
        ledger.transition(root, "b-001", s)
    for sha in "abc":
        ledger.append(root, "batch.pushed", batch="b-001", sha=sha)
    out = status.render(ledger.fold(ledger.read_events(root)), config={"caps": {"pushes": 3}})
    assert "pushes" in out
    assert "needs you" in out.lower()


def test_merged_batches_are_counted_not_listed(root):
    for i in (1, 2):
        bid = f"b-00{i}"
        ledger.append(root, "batch.created", batch=bid, issues=[i])
        for s in ("building", "built", "open"):
            ledger.transition(root, bid, s)
        ledger.gate(root, bid, "ci", "full_green")
        ledger.gate(root, bid, "review", "clean")
        for s in ("ready", "merging", "merged"):
            ledger.transition(root, bid, s)
    out = status.render(ledger.fold(ledger.read_events(root)), config={})
    assert "merged" in out.lower() and "2" in out


def test_flake_leaderboard_surfaces_the_worst_offender(root):
    for _ in range(4):
        ledger.append(root, "flake.observed", job="integration", test="test_login")
    ledger.append(root, "flake.observed", job="unit", test="test_parse")
    out = status.render(ledger.fold(ledger.read_events(root)), config={})
    assert "test_login" in out and "4" in out


def test_review_verdicts_and_post_merge_reverts_are_reported(root):
    """The rubber-stamp signal: clean reviews that later got reverted."""
    ledger.append(root, "review.verdict", batch="b-001", verdict="clean")
    ledger.append(root, "review.verdict", batch="b-002", verdict="clean")
    ledger.append(root, "merge.reverted", batch="b-002", within_days=2)
    out = status.render(ledger.fold(ledger.read_events(root)), config={})
    assert "1/2" in out or "50" in out


def test_an_escalation_stops_nagging_once_the_batch_moves_on(root):
    """NEEDS YOU is the morning report. A resolved item there teaches you to skip it."""
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    ledger.transition(root, "b-001", "building")
    ledger.transition(root, "b-001", "escalated")
    ledger.append(root, "escalation", batch="b-001", reason="pushes at cap")
    assert "pushes at cap" in status.render(ledger.load(root), config={})

    # a human requeues it and it goes on to merge
    ledger.transition(root, "b-001", "planned")
    for step in ("building", "built", "open"):
        ledger.transition(root, "b-001", step)
    ledger.gate(root, "b-001", "ci", "full_green")
    ledger.gate(root, "b-001", "review", "clean")
    for step in ("ready", "merging", "merged"):
        ledger.transition(root, "b-001", step)

    out = status.render(ledger.load(root), config={})
    assert "pushes at cap" not in out, "a merged batch needs nobody"
    assert "Nothing blocked." in out


def test_an_escalation_for_a_batch_still_escalated_is_still_shown(root):
    ledger.append(root, "batch.created", batch="b-002", issues=[2])
    ledger.transition(root, "b-002", "building")
    ledger.transition(root, "b-002", "escalated")
    ledger.append(root, "escalation", batch="b-002", reason="needs a human")
    assert "needs a human" in status.render(ledger.load(root), config={})


# --- issue #59: one name for the ledger directory across every script --------


def test_the_ledger_is_named_ledger_here_as_it_is_in_every_other_script(root, capsys):
    """triage.py, batch.py and loop.py all spell it --ledger, so a shared command
    line has to work here too."""
    ledger.append(root, "batch.created", batch="b-001", issues=[42])
    assert status.main(["--ledger", str(root)]) == 0
    assert "b-001" in capsys.readouterr().out


def test_the_older_root_spelling_still_works(root, capsys):
    """Existing commands and docs pass --root; renaming must not break them."""
    ledger.append(root, "batch.created", batch="b-001", issues=[42])
    assert status.main(["--root", str(root)]) == 0
    assert "b-001" in capsys.readouterr().out


# --- issue #70: the config must not follow the caller around here either -----


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.email=f@example.com", "-c", "user.name=foreman", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


@pytest.fixture
def worktree(tmp_path):
    """A repo plus the build worktree `commands/build.md` prescribes."""
    checkout = tmp_path / "repo"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main")
    _git(checkout, "commit", "-q", "--allow-empty", "-m", "root")
    linked = tmp_path / "foreman-b-001"
    _git(checkout, "worktree", "add", "-q", str(linked), "-b", "foreman/b-001")
    return checkout, linked


def _batch_at_three_pushes(root):
    ledger.append(root, "batch.created", batch="b-001", issues=[1])
    for step in ("building", "built", "open"):
        ledger.transition(root, "b-001", step)
    for sha in "abc":
        ledger.append(root, "batch.pushed", batch="b-001", sha=sha)


def test_the_caps_are_read_from_the_repository_when_status_runs_from_a_worktree(
    worktree, monkeypatch, capsys
):
    """`root / "config.json"` resolved against the caller, so from a build
    worktree the digest rendered with `caps={}` — every counter without its
    ceiling, and no batch ever shown at cap."""
    checkout, linked = worktree
    root = ledger.init(checkout)
    (root / ledger.CONFIG_FILE).write_text(json.dumps({"caps": {"pushes": 3}}))
    _batch_at_three_pushes(root)
    monkeypatch.chdir(linked)

    assert status.main([]) == 0
    out = capsys.readouterr().out
    assert "at cap" in out and "pushes = 3/3" in out


def test_a_missing_config_is_said_out_loud_rather_than_rendered_as_no_caps(
    worktree, monkeypatch, capsys
):
    """Silence was the defect: the digest looked complete with its brakes off."""
    checkout, linked = worktree
    _batch_at_three_pushes(ledger.init(checkout))
    monkeypatch.chdir(linked)

    assert status.main([]) == 0
    captured = capsys.readouterr()
    assert "Nothing blocked." in captured.out, "no config really does mean no caps"
    assert str(checkout / ledger.LEDGER_DIR / ledger.CONFIG_FILE) in captured.err


def test_an_explicit_config_path_is_still_obeyed(worktree, monkeypatch, tmp_path, capsys):
    checkout, linked = worktree
    elsewhere = tmp_path / "strict.json"
    elsewhere.write_text(json.dumps({"caps": {"pushes": 1}}))
    _batch_at_three_pushes(ledger.init(checkout))
    monkeypatch.chdir(linked)

    assert status.main(["--config", str(elsewhere)]) == 0
    assert "pushes = 3/1" in capsys.readouterr().out
