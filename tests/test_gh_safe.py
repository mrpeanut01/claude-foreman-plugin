"""The gh wrapper: an allowlist, so an unattended loop cannot do damage."""

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "gh_safe.sh"


def run(*args, tmp_path=None):
    env = {**os.environ, "FOREMAN_DRY_RUN": "1"}
    if tmp_path:
        env["FOREMAN_LEDGER"] = str(tmp_path)
    return subprocess.run([str(WRAPPER), *args], capture_output=True, text=True, env=env)


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
    ],
)
def test_allowed_operations_pass(args):
    assert run(*args).returncode == 0, f"{args} should be allowed"


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
    run("issue", "view", "42", tmp_path=tmp_path)
    run("repo", "delete", "o/r", tmp_path=tmp_path)
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
    run("issue", "comment", "42", "--body", MARKDOWN_BODY, tmp_path=tmp_path)
    assert len((tmp_path / "gh-audit.log").read_text().splitlines()) == 1
    (record,) = records(tmp_path)
    assert record["argv"] == ["issue", "comment", "42", "--body", MARKDOWN_BODY]


def test_a_body_cannot_forge_a_record_of_a_call_that_never_happened(tmp_path):
    run("issue", "comment", "42", "--body", MARKDOWN_BODY, tmp_path=tmp_path)
    assert [r["decision"] for r in records(tmp_path)] == ["ALLOWED"]


def test_a_refusal_records_the_multi_line_argument_that_provoked_it(tmp_path):
    run("issue", "delete", "42", "--body", MARKDOWN_BODY, tmp_path=tmp_path)
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
    run("issue", "comment", "42", "--body", body, tmp_path=tmp_path)
    (record,) = records(tmp_path)
    assert record["argv"][-1] == body, why


def test_argument_boundaries_survive_so_two_calls_are_never_confused(tmp_path):
    run("issue", "comment", "42", "--body", "one two", tmp_path=tmp_path)
    run("issue", "comment", "42", "--body", "one", "two", tmp_path=tmp_path)
    joined, split = records(tmp_path)
    assert joined["argv"] == ["issue", "comment", "42", "--body", "one two"]
    assert split["argv"] == ["issue", "comment", "42", "--body", "one", "two"]


def test_every_record_keeps_its_utc_timestamp(tmp_path):
    run("issue", "view", "42", tmp_path=tmp_path)
    run("repo", "delete", "o/r", tmp_path=tmp_path)
    for record in records(tmp_path):
        datetime.strptime(record["ts"], "%Y-%m-%dT%H:%M:%SZ")


def test_a_call_with_no_subcommand_is_refused_and_still_audited(tmp_path):
    result = run(tmp_path=tmp_path)
    assert result.returncode != 0
    (record,) = records(tmp_path)
    assert record["decision"] == "REFUSED"
    assert record["argv"] == []
