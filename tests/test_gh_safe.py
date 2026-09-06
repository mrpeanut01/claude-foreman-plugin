"""The gh wrapper: an allowlist, so an unattended loop cannot do damage."""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "gh_safe.sh"
REPO = WRAPPER.parents[1]

# `run` used to set FOREMAN_LEDGER only when a test handed it one. Every other
# call fell back to the wrapper's own default, which is relative, so the suite
# appended a record to the audit log of whichever checkout pytest was run from:
# the tests for the audit log were writing into the repository under test.
# Nothing here may touch a real .foreman/, so a per-test directory is the
# default and a test that wants another location has to name it.
_SANDBOX = object()


@pytest.fixture(autouse=True)
def sandbox_ledger(tmp_path, monkeypatch):
    """Point every call in this module at a throwaway ledger."""
    monkeypatch.setenv("FOREMAN_LEDGER", str(tmp_path / "sandbox"))


def run(*args, ledger=_SANDBOX, cwd=None):
    """The wrapper, dry-run, auditing into `ledger` and running from `cwd`.

    `ledger=None` removes FOREMAN_LEDGER entirely, which is the only way to
    exercise the wrapper's own idea of where the audit log belongs.
    """
    env = {**os.environ, "FOREMAN_DRY_RUN": "1"}
    if ledger is None:
        env.pop("FOREMAN_LEDGER", None)
    elif ledger is not _SANDBOX:
        env["FOREMAN_LEDGER"] = str(ledger)
    return subprocess.run([str(WRAPPER), *args], capture_output=True, text=True, env=env, cwd=cwd)


def contents(root):
    """Every file under `root`, so a write nobody asked for is visible."""
    if not root.is_dir():
        return None
    return {p.relative_to(root): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_no_call_in_this_suite_writes_to_the_repositorys_own_ledger():
    """A call with no ledger of its own, from the repository, is the leaking shape."""
    before = contents(REPO / ".foreman")
    run("issue", "view", "42", cwd=REPO)
    run("repo", "delete", "o/r", cwd=REPO)
    assert contents(REPO / ".foreman") == before


@pytest.mark.parametrize(
    "args",
    [
        ("issue", "view", "42"),
        ("issue", "list", "--state", "open"),
        ("issue", "comment", "42", "--body", "hi"),
        ("pr", "view", "7"),
        ("pr", "create", "--title", "x", "--body", "y"),
        ("pr", "merge", "7", "--auto", "--squash"),
        ("run", "rerun", "123", "--failed"),
        ("run", "view", "123", "--log-failed"),
        ("api", "repos/o/r/branches/main/protection"),
        ("api", "-X", "GET", "repos/o/r"),
        ("api", "-XGET", "repos/o/r"),
        ("api", "--method=GET", "repos/o/r"),
        ("api", "-H", "Accept: application/vnd.github+json", "repos/o/r"),
        ("api", "--paginate", "--jq", ".[].name", "repos/o/r/issues"),
        ("api", "-q", "-.name", "repos/o/r"),
        ("api", "repos/o/r/commits/abc123/check-runs?per_page=100"),
    ],
)
def test_allowed_operations_pass(args):
    assert run(*args).returncode == 0, f"{args} should be allowed"


# pflag, which `gh` is built on, reads `--flag=value` and `-Xvalue` exactly as
# it reads the two-token spellings. The wrapper knew only the two-token ones,
# so every mutation below went through under another spelling.
@pytest.mark.parametrize(
    "args,why",
    [
        (("api", "-XDELETE", "repos/o/r/issues/1"), "joined short flag"),
        (("api", "--method=DELETE", "repos/o/r/issues/1"), "equals form"),
        (("api", "-X=DELETE", "repos/o/r/issues/1"), "short flag with equals"),
        (("api", "--method=PUT", "repos/o/r/branches/main/protection"), "rewrites protection"),
        (("api", "-F", "title=x", "repos/o/r/issues"), "-F is a typed body field"),
        (("api", "-Ftitle=x", "repos/o/r/issues"), "joined typed field"),
        (("api", "--field=title=x", "repos/o/r/issues"), "equals typed field"),
        (("api", "-ftitle=x", "repos/o/r/issues"), "joined raw field"),
        (("api", "--raw-field=title=x", "repos/o/r/issues"), "equals raw field"),
        (("api", "--input=body.json", "repos/o/r/issues"), "equals body file"),
        (("api", "--input", "-", "repos/o/r/issues"), "body from stdin"),
        (("api", "graphql", "-F", "query=mutation{x}"), "a GraphQL mutation is a write"),
        (("api", "--no-such-flag", "repos/o/r"), "an unknown flag might carry a body"),
        (("pr", "merge", "7", "--admin=true"), "equals form of --admin"),
        (("pr", "merge", "7", "--squash", "--admin=1"), "any value of --admin"),
    ],
)
def test_every_spelling_of_a_mutation_is_refused(args, why):
    result = run(*args)
    assert result.returncode != 0, f"{args} must be refused: {why}"
    assert "refused" in (result.stderr + result.stdout).lower()


def test_a_value_that_starts_with_a_dash_is_a_value_not_a_flag():
    """`--jq -.name` is a legal jq program; refusing its value would refuse the read."""
    assert run("api", "--jq", "-.name", "repos/o/r").returncode == 0
    assert run("api", "-H", "-weird-header", "repos/o/r").returncode == 0


@pytest.mark.parametrize(
    "args,why",
    [
        (("issue", "delete", "42"), "deleting an issue is unrecoverable"),
        (("repo", "delete", "o/r"), "never"),
        (("api", "-X", "DELETE", "repos/o/r"), "no destructive api verbs"),
        (("api", "--method", "DELETE", "repos/o/r"), "no destructive api verbs"),
        (
            ("api", "-X", "PUT", "repos/o/r/branches/main/protection"),
            "no editing branch protection",
        ),
        (("pr", "merge", "7", "--admin"), "admin merge bypasses the gates the loop depends on"),
        (("release", "create", "v1"), "not in the loop's job"),
        (("auth", "token"), "never print credentials"),
    ],
)
def test_denied_operations_are_refused(args, why):
    result = run(*args)
    assert result.returncode != 0, f"{args} must be refused: {why}"
    assert "refused" in (result.stderr + result.stdout).lower()


def records(tmp_path):
    """The audit log, parsed back. One JSON object per line, oldest first."""
    lines = (tmp_path / "gh-audit.log").read_text().splitlines()
    return [json.loads(line) for line in lines]


def test_every_call_is_audited(tmp_path):
    run("issue", "view", "42", ledger=tmp_path)
    run("repo", "delete", "o/r", ledger=tmp_path)
    allowed, refused = records(tmp_path)
    assert allowed["decision"] == "ALLOWED"
    assert allowed["argv"] == ["issue", "view", "42"]
    assert refused["decision"] == "REFUSED"
    assert refused["argv"] == ["repo", "delete", "o/r"]
    assert "unrecoverable" in refused["reason"]


# The bug this format exists to prevent. `gh issue create --body` and
# `gh pr create --body` both take multi-line markdown, so a log that reserves
# the newline as its record separator is corrupt on the first real call.
MARKDOWN_BODY = """## Summary

The wrapper refused nothing here.

| check | result |
| ----- | ------ |
| ci    | green  |

2026-01-01T00:00:00Z\tREFUSED\trepo delete o/r\tforged
"""


def test_a_multi_line_body_stays_on_one_line_in_the_audit_log(tmp_path):
    run("issue", "comment", "42", "--body", MARKDOWN_BODY, ledger=tmp_path)
    assert len((tmp_path / "gh-audit.log").read_text().splitlines()) == 1
    (record,) = records(tmp_path)
    assert record["argv"] == ["issue", "comment", "42", "--body", MARKDOWN_BODY]


def test_a_body_cannot_forge_a_record_of_a_call_that_never_happened(tmp_path):
    run("issue", "comment", "42", "--body", MARKDOWN_BODY, ledger=tmp_path)
    assert [r["decision"] for r in records(tmp_path)] == ["ALLOWED"]


def test_a_refusal_records_the_multi_line_argument_that_provoked_it(tmp_path):
    run("issue", "delete", "42", "--body", MARKDOWN_BODY, ledger=tmp_path)
    (record,) = records(tmp_path)
    assert record["decision"] == "REFUSED"
    assert record["argv"] == ["issue", "delete", "42", "--body", MARKDOWN_BODY]


@pytest.mark.parametrize(
    "body,why",
    [
        ('a "quoted" phrase', "double quotes close the JSON string"),
        ("a\\backslash and a \\", "backslashes start an escape sequence"),
        ("tab\tseparated\tvalues", "the field separator of the format this replaces"),
        ("carriage\rreturn", "a lone CR still moves the cursor to column zero"),
        ("ansi \x1b[31mred\x1b[0m", "captured terminal output carries escape codes"),
        ("unicode: \u2014 \U0001f600 \u00e9", "argv is bytes, and the log is read by humans"),
        ("", "an empty argument is still an argument"),
    ],
)
def test_an_argument_round_trips_through_the_audit_log(body, why, tmp_path):
    run("issue", "comment", "42", "--body", body, ledger=tmp_path)
    (record,) = records(tmp_path)
    assert record["argv"][-1] == body, why


def test_argument_boundaries_survive_so_two_calls_are_never_confused(tmp_path):
    run("issue", "comment", "42", "--body", "one two", ledger=tmp_path)
    run("issue", "comment", "42", "--body", "one", "two", ledger=tmp_path)
    joined, split = records(tmp_path)
    assert joined["argv"] == ["issue", "comment", "42", "--body", "one two"]
    assert split["argv"] == ["issue", "comment", "42", "--body", "one", "two"]


def test_every_record_keeps_its_utc_timestamp(tmp_path):
    run("issue", "view", "42", ledger=tmp_path)
    run("repo", "delete", "o/r", ledger=tmp_path)
    written = records(tmp_path)
    # Two calls, two records. Without this line the loop below is vacuous: a
    # wrapper that stopped writing the log would pass it with nothing to check.
    assert len(written) == 2
    for record in written:
        datetime.strptime(record["ts"], "%Y-%m-%dT%H:%M:%SZ")


def test_a_call_with_no_subcommand_is_refused_and_still_audited(tmp_path):
    result = run(ledger=tmp_path)
    assert result.returncode != 0
    (record,) = records(tmp_path)
    assert record["decision"] == "REFUSED"
    assert record["argv"] == []


# --- where the audit log lands ------------------------------------------------
# `commands/build.md` has a build work in `../foreman-<batch>`, a linked
# worktree that `git worktree remove` deletes when the batch lands. A
# cwd-relative audit log therefore names a directory with a shorter life than
# the record it holds (issue #71).


def git(*args, cwd):
    """git with an identity of its own, so a machine with no gitconfig can commit."""
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=foreman",
            "-c",
            "user.email=foreman@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def build(tmp_path):
    """A checkout and a linked worktree of it: the layout a build runs in."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git("init", "-q", cwd=checkout)
    git("commit", "-q", "--allow-empty", "-m", "root", cwd=checkout)
    worktree = tmp_path / "foreman-b-001"
    git("worktree", "add", "-q", str(worktree), "-b", "b-001", cwd=checkout)
    return checkout, worktree


def test_a_call_from_a_build_worktree_is_audited_in_the_repository(build):
    checkout, worktree = build
    run("issue", "comment", "42", "--body", "done", ledger=None, cwd=worktree)
    (record,) = records(checkout / ".foreman")
    assert record["argv"] == ["issue", "comment", "42", "--body", "done"]
    assert not (worktree / ".foreman").exists(), "an audit log the batch takes with it"


def test_a_refusal_from_a_build_worktree_is_audited_in_the_repository(build):
    checkout, worktree = build
    result = run("repo", "delete", "o/r", ledger=None, cwd=worktree)
    assert result.returncode != 0
    (record,) = records(checkout / ".foreman")
    assert record["decision"] == "REFUSED"


def test_a_call_from_a_subdirectory_reaches_the_same_audit_log(build):
    checkout, worktree = build
    inside = worktree / "scripts"
    inside.mkdir()
    run("issue", "view", "42", ledger=None, cwd=inside)
    assert [r["argv"] for r in records(checkout / ".foreman")] == [["issue", "view", "42"]]


def test_an_absolute_ledger_is_obeyed_verbatim(build, tmp_path):
    """How a caller says "this ledger, not the one you would have picked"."""
    checkout, worktree = build
    elsewhere = tmp_path / "elsewhere"
    run("issue", "view", "42", ledger=elsewhere, cwd=worktree)
    assert [r["argv"] for r in records(elsewhere)] == [["issue", "view", "42"]]
    assert not (checkout / ".foreman").exists()


def test_a_relative_ledger_is_anchored_to_the_repository_like_the_python_ledger(build):
    """`ledger.resolve_root` anchors a relative path to the repo; so does this."""
    checkout, worktree = build
    run("issue", "view", "42", ledger="audit", cwd=worktree)
    assert [r["argv"] for r in records(checkout / "audit")] == [["issue", "view", "42"]]
    assert not (worktree / "audit").exists()


def test_a_directory_in_no_repository_still_gets_an_audit_log(tmp_path):
    """Not every caller is in a checkout; a directory is still a fine place for a log."""
    loose = tmp_path / "loose"
    loose.mkdir()
    run("issue", "view", "42", ledger=None, cwd=loose)
    assert [r["argv"] for r in records(loose / ".foreman")] == [["issue", "view", "42"]]
