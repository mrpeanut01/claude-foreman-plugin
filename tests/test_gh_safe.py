"""The gh wrapper: an allowlist, so an unattended loop cannot do damage."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "gh_safe.sh"


def run(*args, tmp_path=None):
    env = {**os.environ, "FOREMAN_DRY_RUN": "1"}
    if tmp_path:
        env["FOREMAN_LEDGER"] = str(tmp_path)
    return subprocess.run([str(WRAPPER), *args], capture_output=True, text=True, env=env)


@pytest.mark.parametrize("args", [
    ("issue", "view", "42"),
    ("issue", "list", "--state", "open"),
    ("issue", "comment", "42", "--body", "hi"),
    ("pr", "view", "7"),
    ("pr", "create", "--title", "x", "--body", "y"),
    ("pr", "merge", "7", "--auto", "--squash"),
    ("run", "rerun", "123", "--failed"),
    ("run", "view", "123", "--log-failed"),
    ("api", "repos/o/r/branches/main/protection"),
])
def test_allowed_operations_pass(args):
    assert run(*args).returncode == 0, f"{args} should be allowed"


@pytest.mark.parametrize("args,why", [
    (("issue", "delete", "42"), "deleting an issue is unrecoverable"),
    (("repo", "delete", "o/r"), "never"),
    (("api", "-X", "DELETE", "repos/o/r"), "no destructive api verbs"),
    (("api", "--method", "DELETE", "repos/o/r"), "no destructive api verbs"),
    (("api", "-X", "PUT", "repos/o/r/branches/main/protection"), "no editing branch protection"),
    (("pr", "merge", "7", "--admin"), "admin merge bypasses the gates the loop depends on"),
    (("release", "create", "v1"), "not in the loop's job"),
    (("auth", "token"), "never print credentials"),
])
def test_denied_operations_are_refused(args, why):
    result = run(*args)
    assert result.returncode != 0, f"{args} must be refused: {why}"
    assert "refused" in (result.stderr + result.stdout).lower()


def test_every_call_is_audited(tmp_path):
    run("issue", "view", "42", tmp_path=tmp_path)
    run("repo", "delete", "o/r", tmp_path=tmp_path)
    audit = (tmp_path / "gh-audit.log").read_text()
    assert "issue view 42" in audit
    assert "REFUSED" in audit and "repo delete" in audit
