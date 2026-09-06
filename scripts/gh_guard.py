#!/usr/bin/env python3
"""PreToolUse hook: a bare `gh` in a Bash command is held to gh_safe.sh's rules.

The wrapper is a boundary only if nothing goes round it, and every command and
the reviewer run with an unscoped `Bash`. Subagent frontmatter cannot scope a
tool to a command; a hook is the place Claude Code documents for that. So this
asks two questions of every Bash call: is foreman in use in this checkout — a
`.foreman` directory at the repository root — and would the wrapper have
refused any `gh` invocation in the command? A call the wrapper allows goes
through untouched, and the wrapper's audit log gets the record either way.

Best effort by design, and fail-open. The command string is split the way the
gate splits one — on `;`, `&&`, `||`, `|` and newlines, then shlex — so a `gh`
inside `$(...)` is not seen. That is the backstop's limit, not the boundary's:
the recipes route every write through the wrapper, and this exists to catch
the one that did not.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger  # noqa: E402

WRAPPER = Path(__file__).resolve().parent / "gh_safe.sh"
_ASSIGNMENT = re.compile(r"^\w+=")
_OPERATOR = re.compile(r"^[;&|()]+$")
PREFIXES = {"sudo", "env", "command", "nohup", "exec", "time"}


def _lex(text: str) -> list[str]:
    """Shell words, with `;`, `&&`, `||`, `|` and parentheses as words of their own.

    Lexing before splitting is the point: cutting the text on `;` first tore
    `--body "a; b"` in half, and the half with the unbalanced quote was dropped
    along with the `gh` in front of it.
    """
    lexer = shlex.shlex(text, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def _commands(command: str) -> list[list[str]]:
    """Each simple command's words. Lines are lexed one at a time, so commands on
    separate lines stay separate; a line that will not lex alone (a quote or a
    heredoc spanning lines) falls back to lexing the whole text at once."""
    try:
        streams = [_lex(line) for line in command.splitlines() if line.strip()]
    except ValueError:
        try:
            streams = [_lex(command)]
        except ValueError:
            return []  # the shell will refuse it before gh ever runs
    segments: list[list[str]] = []
    for tokens in streams:
        current: list[str] = []
        for token in tokens:
            if _OPERATOR.match(token):
                segments.append(current)
                current = []
            else:
                current.append(token)
        segments.append(current)
    return [s for s in segments if s]


def gh_invocations(command: str) -> list[list[str]]:
    """The argv after `gh` for every simple command that runs it, best effort."""
    found = []
    for tokens in _commands(command):
        while tokens and (_ASSIGNMENT.match(tokens[0]) or tokens[0] in PREFIXES):
            tokens.pop(0)
        if tokens and (tokens[0] == "gh" or tokens[0].endswith("/gh")):
            found.append(tokens[1:])
    return found


def foreman_ledger(cwd: str) -> Path | None:
    """The repository's `.foreman`, when foreman is in use here; else None."""
    root = ledger.repo_root(cwd) / ledger.LEDGER_DIR
    return root if root.is_dir() else None


def refusal(args: list[str], audit_dir: Path) -> str | None:
    """The wrapper's reason for refusing this call, or None when it allows it."""
    env = {**os.environ, "FOREMAN_DRY_RUN": "1", "FOREMAN_LEDGER": str(audit_dir)}
    try:
        done = subprocess.run([str(WRAPPER), *args], capture_output=True, text=True, env=env)
    except OSError:
        return None  # no shell, no wrapper: nothing to hold the call to
    if done.returncode == 0:
        return None
    lines = [line for line in done.stderr.splitlines() if line.strip()]
    return lines[0] if lines else "refused by gh_safe.sh"


def decide(payload: dict) -> dict | None:
    """A deny decision for the hook to print, or None to let the call through."""
    if payload.get("tool_name") != "Bash":
        return None
    command = str((payload.get("tool_input") or {}).get("command") or "")
    calls = gh_invocations(command)
    if not calls:
        return None
    audit_dir = foreman_ledger(payload.get("cwd") or os.getcwd())
    if audit_dir is None:
        return None
    for args in calls:
        why = refusal(args, audit_dir)
        if why:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"foreman: {why} Route GitHub writes through "
                        f"{WRAPPER}, which allows only what the loop needs."
                    ),
                }
            }
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    decision = decide(payload)
    if decision:
        print(json.dumps(decision))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
