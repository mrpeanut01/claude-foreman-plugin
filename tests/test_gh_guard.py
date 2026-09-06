"""The hook that holds a bare `gh` to the wrapper's rules."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import gh_guard  # noqa: E402

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "gh_guard.py"
HOOKS = Path(__file__).resolve().parents[1] / "hooks" / "hooks.json"


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def foreman_repo(tmp_path):
    """A checkout foreman is in use on: a repository with a .foreman directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    (repo / ".foreman").mkdir()
    return repo


@pytest.fixture
def plain_repo(tmp_path):
    repo = tmp_path / "plain"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    return repo


def run_hook(command, cwd, tool="Bash"):
    """The hook as Claude Code runs it: JSON on stdin, a decision on stdout."""
    payload = {"tool_name": tool, "tool_input": {"command": command}, "cwd": str(cwd)}
    done = subprocess.run(
        [sys.executable, str(GUARD)], input=json.dumps(payload), capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout) if done.stdout.strip() else None


def denied(decision):
    return decision is not None and decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_call_the_wrapper_would_refuse_is_denied(foreman_repo):
    decision = run_hook("gh issue delete 5", foreman_repo)
    assert denied(decision)
    assert "refused" in decision["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_call_the_wrapper_allows_goes_through(foreman_repo):
    assert run_hook("gh issue view 5 --repo o/r", foreman_repo) is None


@pytest.mark.parametrize(
    "command",
    [
        "python3 x.py && gh api -XDELETE repos/o/r/issues/1",
        "gh pr merge 7 --admin=true; echo done",
        "env GH_TOKEN=x gh repo delete o/r",
        "/usr/local/bin/gh api --method=PUT repos/o/r/branches/main/protection",
        "ls | gh issue delete 5",
    ],
)
def test_a_refused_call_anywhere_in_the_command_is_denied(foreman_repo, command):
    assert denied(run_hook(command, foreman_repo))


def test_a_command_with_no_gh_in_it_is_not_the_hooks_business(foreman_repo):
    assert run_hook("git status && python3 -m pytest", foreman_repo) is None


def test_the_hook_is_inert_where_foreman_is_not_in_use(plain_repo, tmp_path):
    assert run_hook("gh issue delete 5", plain_repo) is None
    loose = tmp_path / "loose"
    loose.mkdir()
    assert run_hook("gh issue delete 5", loose) is None


def test_the_hook_is_inert_for_other_tools(foreman_repo):
    assert run_hook("gh issue delete 5", foreman_repo, tool="Read") is None


def test_a_call_from_a_subdirectory_is_still_held_to_the_rules(foreman_repo):
    inside = foreman_repo / "src"
    inside.mkdir()
    assert denied(run_hook("gh issue delete 5", inside))


def test_the_refusal_reaches_the_wrappers_audit_log(foreman_repo):
    run_hook("gh issue delete 5", foreman_repo)
    log = (foreman_repo / ".foreman" / "gh-audit.log").read_text().splitlines()
    record = json.loads(log[-1])
    assert record["decision"] == "REFUSED"
    assert record["argv"] == ["issue", "delete", "5"]


def test_malformed_input_lets_the_call_through_rather_than_crashing():
    done = subprocess.run(
        [sys.executable, str(GUARD)], input="not json", capture_output=True, text=True
    )
    assert done.returncode == 0 and done.stdout == ""


def test_gh_invocations_are_found_past_prefixes_and_assignments():
    calls = gh_guard.gh_invocations
    assert calls("FOO=1 sudo gh api x") == [["api", "x"]]
    assert calls("echo hi") == []


def test_a_quoted_argument_holding_an_operator_is_not_split_on_it():
    """Cutting on `;` before lexing tore `--body "a; b"` in half and dropped
    the `gh` in front of it with the unbalanced half."""
    calls = gh_guard.gh_invocations
    assert calls('gh issue comment 1 --body "a; b && c"') == [
        ["issue", "comment", "1", "--body", "a; b && c"]
    ]


def test_commands_on_separate_lines_are_separate_commands():
    calls = gh_guard.gh_invocations
    assert calls("gh issue view 1\ngh issue delete 2") == [
        ["issue", "view", "1"],
        ["issue", "delete", "2"],
    ]


def test_a_command_the_shell_would_refuse_is_left_to_the_shell():
    assert gh_guard.gh_invocations('gh issue delete "unterminated') == []


def test_the_plugin_wires_the_hook_to_bash():
    hooks = json.loads(HOOKS.read_text())
    (entry,) = hooks["hooks"]["PreToolUse"]
    assert entry["matcher"] == "Bash"
    assert entry["hooks"][0]["command"].endswith("scripts/gh_guard.py")
