#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Run the whole local gate as one command, so that it cannot be half-run.

The gate used to be a checklist: map the diff to its tests, run those, then lint,
then typecheck, then push. Prose cannot be executed, and in practice the last
items got skipped — a lint step that was never run looks exactly like a lint step
that passed. This turns the checklist into a command with one guarantee: it exits
zero only when every check CI would run on this change was executed here and
passed. Everything else, it names.

Where the commands come from: the cheap-tier jobs in .foreman/ci-profile.json.
Their `run:` steps ARE the gate — they are the exact commands CI will run, so
inventing an equivalent locally only produces a gate that agrees with itself.
Ahead of them the gate runs the tests covering the diff (ci_profile.py impact),
because the fastest way to learn a change is broken should not sit behind a
linter.

Four rules keep a green from lying:

  * A diff the gate could not read is not a diff with nothing in it. An unknown
    base ref exits 2 (`blocked`), because a change nobody could see requires no
    test, and a gate that required no test runs none and still exits 0.
  * A step that could not run is not a step that passed. A missing tool exits 2
    (`blocked`), never 0 — that silence is precisely what this exists to stop.
    `--allow-unrunnable` overrides it for a machine that genuinely lacks the
    tool: it exits 0 and prints every check it let through unrun, so the choice
    to let CI be the first to run that check is made out loud, by a person.
  * A step that verifies nothing — a GitHub Action, an install command — is
    skipped and said out loud, never dropped.
  * A gate that ran no test over changed code is `blocked`, not green.

Running this executes the repo's own CI commands on this machine, which means the
gate trusts the checkout exactly as much as CI does.

CLI:
    gate.py run  [--root .] [--base main] [--changed FILE ...] [--profile PATH]
                 [--workflows DIR] [--test-command CMD] [--allow-unrunnable]
                 [--keep-going]
    gate.py plan [same flags]     # what it would run, without running it

Exit: 0 green (or waived), 1 a check failed, 2 the gate could not complete.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from functools import cache
from pathlib import Path

import yaml

# scripts/ is not an installed package: put it on the path, then load by name.
sys.path.insert(0, str(Path(__file__).resolve().parent))
ci_profile = importlib.import_module("ci_profile")
batch = importlib.import_module("batch")
ledger = importlib.import_module("ledger")

# batch.py already had to answer "what did this branch change?" and already had
# to refuse to answer it with silence. Reusing its exception rather than growing
# a second one keeps that refusal a single idea: wherever it is raised, it means
# the diff could not be determined, which is never the same claim as an empty one.
PathsUnavailable = batch.PathsUnavailable

# Events that put a job on a pull request. A job wired only to push, schedule or
# release may publish, tag or deploy; running one on a laptop is not a gate, it
# is an accident. So the gate only ever runs what a pull request itself triggers.
PR_EVENTS = {"pull_request", "pull_request_target"}

# Tiers worth paying for locally. `expensive` is what CI's parallelism is for and
# the whole doctrine is to not pay it twice. `unmeasured` is included on purpose:
# missing cost data is not evidence that a job is slow, and dropping a check
# because nobody has measured it yet is the failure this script exists to stop.
LOCAL_TIERS = {"cheap", "unmeasured", "unknown"}

# Commands that provision rather than verify. Skipping one cannot turn a red into
# a green — it checks nothing — and if it leaves a tool absent, that surfaces as
# a missing tool below. Running one would rewrite the developer's machine to look
# like a CI runner, which is not a thing a gate may do to you.
PROVISIONING = re.compile(
    r"""(?x)
      \b (?: pip3? | python3?\s+-m\s+pip ) \s+ install \b
    | \b uv \s+ (?: pip \s+ install | sync | venv ) \b
    | \b (?: poetry | pipenv | composer | bundle ) \s+ install \b
    | \b npm \s+ (?: ci | install | i ) \b
    | \b (?: yarn | pnpm ) \s+ (?: install | add ) \b
    | \b go \s+ mod \s+ (?: download | tidy ) \b
    | \b cargo \s+ fetch \b
    | \b (?: apt-get | apt | brew | gem | apk | dnf | yum ) \s+ (?: install | add ) \b
    """
)

# Whether a step is a test run. The gate needs to know, because a green that
# never executed a test over changed code is not a gate result, it is an opinion.
TEST_RUNNER = re.compile(
    r"""(?x)
      \b (?: pytest | unittest | tox | nox ) \b
    | \b (?: jest | vitest | mocha | ava | karma | rspec | phpunit ) \b
    | \b (?: npm | yarn | pnpm ) \s+ (?: run \s+ )? test \b
    | \b go \s+ test \b | \b cargo \s+ test \b | \b dotnet \s+ test \b
    | \b (?: gradle | mvn | make ) \s+ \S*test \b
    """
)

# `${{ ... }}` is substituted by Actions, out of context no laptop has.
GITHUB_EXPRESSION = re.compile(r"\$\{\{.*?\}\}")

# Words that name no program, or that every shell supplies. Looking these up on
# PATH produces false "missing tool" reports, which train people to pass
# --allow-unrunnable by reflex — and a reflex waiver is no gate at all.
SHELL_WORDS = {
    "!",
    "[",
    "[[",
    ".",
    ":",
    "break",
    "case",
    "cd",
    "continue",
    "do",
    "done",
    "echo",
    "elif",
    "else",
    "esac",
    "eval",
    "exec",
    "exit",
    "export",
    "false",
    "fi",
    "for",
    "function",
    "if",
    "in",
    "local",
    "printf",
    "pwd",
    "read",
    "return",
    "set",
    "shift",
    "source",
    "test",
    "then",
    "time",
    "trap",
    "true",
    "umask",
    "unset",
    "until",
    "wait",
    "while",
}

# `sudo` and friends prefix a command without being the command.
PREFIXES = {"sudo", "env", "command", "nohup", "exec", "xargs", "time"}

_SEGMENT = re.compile(r"\|\||&&|[;|\n]")
_ASSIGNMENT = re.compile(r"^\w+=")
_INTERPRETER = re.compile(r"^python[\d.]*$")
_BARE_PYTHON = re.compile(r"(?<![\w./-])python(?![\w.-])")

OUTPUT_TAIL = 4000


# --- what this machine can actually run ---------------------------------------


def _segments(command: str) -> list[list[str]]:
    """The argv of each command in a shell snippet, best effort.

    Best effort is the right standard here: this parse only decides whether to
    look something up on PATH, and whatever it cannot parse falls through to the
    shell, which is the real authority.
    """
    parsed = []
    for segment in _SEGMENT.split(command):
        try:
            tokens = shlex.split(segment.strip(), comments=True)
        except ValueError:
            continue  # unbalanced quotes: let the shell be the judge
        while tokens and (_ASSIGNMENT.match(tokens[0]) or tokens[0] in PREFIXES):
            tokens.pop(0)
        if tokens:
            parsed.append(tokens)
    return parsed


@cache
def _importable(interpreter: str, module: str) -> bool:
    probe = "\n".join(
        [
            "import importlib.util as u, sys",
            "try:",
            f"    found = u.find_spec({module!r}) is not None",
            "except Exception:",
            "    found = False",
            "sys.exit(0 if found else 1)",
        ]
    )
    try:
        done = subprocess.run([interpreter, "-c", probe], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return True  # cannot tell; let the step run and report for itself
    return done.returncode == 0


def missing_requirements(command: str) -> list[str]:
    """What this command needs and this machine does not have.

    Two kinds of absence, because the incident behind #47 involved both: an
    executable that is not on PATH, and a module that is not importable by the
    interpreter that would run it — `python -m ruff` on a machine where ruff is
    only reachable through `uvx`. Both used to read as "the check passed".
    """
    missing = []
    for tokens in _segments(command):
        program = tokens[0]
        if program in SHELL_WORDS:
            continue
        if shutil.which(program) is None:
            missing.append(f"`{program}` is not on PATH")
            continue
        interpreted = len(tokens) >= 3 and tokens[1] == "-m"
        if interpreted and _INTERPRETER.match(Path(program).name):
            if not _importable(program, tokens[2]):
                missing.append(f"`{program}` cannot import `{tokens[2]}`")
    return missing


def adapt_command(command: str) -> tuple[str, str | None]:
    """Adjust a CI command for this machine, and say so whenever it changed one.

    Exactly one adjustment is allowed: `python` becomes `python3`. The
    setup-python action puts a bare `python` on every runner; laptops routinely
    have only `python3`, and a gate that blocks on that for every Python repo is
    a gate nobody runs. Anything past this single alias would be the gate
    inventing commands, and an invented command no longer predicts CI.
    """
    if not _BARE_PYTHON.search(command):
        return command, None
    if shutil.which("python") or not shutil.which("python3"):
        return command, None
    return _BARE_PYTHON.sub("python3", command), "ran with python3: no `python` on PATH"


# --- choosing the jobs whose steps make up the gate ---------------------------


def _on_pull_request(spec: dict) -> bool:
    """Newer profiles carry a full `events` map; older ones only a trigger list."""
    declared = set(spec.get("events") or {}) or set(spec.get("triggers") or [])
    return bool(declared & PR_EVENTS)


def select_jobs(
    profile: dict | None, workflow_dir: Path
) -> tuple[list[dict], list[dict], list[str]]:
    """The jobs the gate will run, the ones deliberately left to CI, and why."""
    notes: list[str] = []
    if profile and profile.get("jobs"):
        source = [
            {
                "name": name,
                "workflow_file": spec.get("workflow_file"),
                "tier": spec.get("tier") or "unmeasured",
                "events": spec.get("events"),
                "triggers": spec.get("triggers"),
            }
            for name, spec in profile["jobs"].items()
        ]
    else:
        notes.append(
            "no CI profile: tiers are unknown, so every pull-request job is included. "
            "Run `ci_profile.py probe` to learn what each job costs."
        )
        source = [{**job, "tier": "unknown"} for job in ci_profile.parse_workflows(workflow_dir)]

    chosen, deferred = [], []
    for job in source:
        if not _on_pull_request(job):
            continue
        if job["tier"] not in LOCAL_TIERS:
            deferred.append(
                {
                    "job": job["name"],
                    "reason": f"{job['tier']} tier: CI has parallelism a laptop does not",
                }
            )
            continue
        chosen.append(job)

    unmeasured = sorted(j["name"] for j in chosen if j["tier"] == "unmeasured")
    if unmeasured:
        notes.append(f"unmeasured, so included without cost data: {', '.join(unmeasured)}")
    return chosen, deferred, notes


@cache
def _load_workflow(path: str) -> dict:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return doc if isinstance(doc, dict) else {}


def job_spec(workflow_dir: Path, job: dict) -> tuple[dict, dict, str | None]:
    """The workflow and the job's own mapping, or the reason they cannot be read.

    A profile is a snapshot and workflows move underneath it. Not knowing a job's
    commands is not the same as that job having none, so this reports the
    difference rather than returning an empty mapping for both.
    """
    name = job["name"]
    filename = job.get("workflow_file")
    if not filename:
        return {}, {}, f"the profile records no workflow file for `{name}`"
    path = Path(workflow_dir) / filename
    if not path.is_file():
        return {}, {}, f"`{name}` is declared in {filename}, which is not there any more"
    try:
        doc = _load_workflow(str(path))
    except (OSError, yaml.YAMLError) as exc:
        return {}, {}, f"{filename} could not be read ({type(exc).__name__})"
    spec = (doc.get("jobs") or {}).get(name)
    if not isinstance(spec, dict):
        return doc, {}, f"{filename} no longer declares a job named `{name}` (stale profile?)"
    return doc, spec, None


def job_steps(workflow_dir: Path, job: dict) -> tuple[list[dict], str | None]:
    """The declared steps of one job, or the reason they cannot be read."""
    _, spec, problem = job_spec(workflow_dir, job)
    if problem:
        return [], problem
    return [step for step in (spec.get("steps") or []) if isinstance(step, dict)], None


def _env_text(value: object) -> str:
    """An `env:` value the way Actions hands it to the step: booleans lower-case."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def job_context(doc: dict, spec: dict) -> dict:
    """What every step of a job inherits before its own keys apply.

    `env:` and `defaults.run.working-directory` are set at workflow level and at
    job level at least as often as on the step, and Actions folds all three
    together for each step. The gate read only the step's own block, so a job
    whose steps relied on `env: {FOO: bar}` one level up failed here with a red
    CI would never produce — or passed with one CI would never produce, when
    the variable gated behaviour.

    A `${{ }}` expression in an inherited value is Actions context no laptop
    has. It is left out rather than passed through unsubstituted, and named
    under `env_dropped`, so a step that reads it can be blocked by `plan_step`
    rather than run against a variable that is silently unset.
    """
    env: dict[str, str] = {}
    dropped: list[str] = []
    for scope in (doc.get("env"), spec.get("env")):
        if not isinstance(scope, dict):
            continue
        for key, value in scope.items():
            text = _env_text(value)
            if GITHUB_EXPRESSION.search(text):
                dropped.append(str(key))
                env.pop(str(key), None)
            else:
                env[str(key)] = text
    working_directory = None
    for scope in (doc.get("defaults"), spec.get("defaults")):
        run = scope.get("run") if isinstance(scope, dict) else None
        if isinstance(run, dict) and run.get("working-directory"):
            working_directory = str(run["working-directory"])
    return {
        "env": env,
        "env_dropped": dropped,
        "working_directory": working_directory,
        "continue_on_error": bool(spec.get("continue-on-error")),
    }


# --- turning steps into a plan ------------------------------------------------


def _blocked(entry: dict, reason: str) -> dict:
    return {**entry, "action": "block", "reason": reason}


def _skipped(entry: dict, reason: str) -> dict:
    return {**entry, "action": "skip", "reason": reason}


def _runnable(entry: dict) -> dict:
    """The last question before a step is allowed to run: can this machine run it?"""
    adapted, note = adapt_command(entry["command"])
    if note:
        entry = {**entry, "command": adapted, "adapted": note, "declared": entry["command"]}
    missing = missing_requirements(adapted)
    if missing:
        return _blocked(entry, "; ".join(missing))
    return {**entry, "action": "run"}


def _entry(step_id: str, job: str, **overrides) -> dict:
    return {
        "id": step_id,
        "job": job,
        "name": None,
        "command": None,
        "kind": "check",
        "continue_on_error": False,
        "working_directory": None,
        "env": {},
        **overrides,
    }


def run_text(value: object) -> str:
    """The shell command a `run:` value denotes, or "" when it denotes none.

    Actions types `run:` as a string, so `run: true` is the command `true`. YAML
    does not know that, and PyYAML resolves an unquoted `true`, `on` or `no` to a
    Python bool; `str()` on one of those yields `True`, which names a program no
    machine has. The gate then reported an ordinary no-op step as a missing tool
    — and worse, hid it: on a case-insensitive filesystem `which("True")` finds
    /usr/bin/true, so this was green on macOS and blocked on Linux.

    So undo the resolution rather than stringify the artefact: a scalar renders
    the way the author wrote it, booleans lower-case. A list or a mapping is not
    a command in any shell; it gets "" and the caller blocks, because inventing
    one would be the gate guessing at what CI runs.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _reads_variable(command: str, name: str) -> bool:
    return re.search(rf"\$\{{?{re.escape(name)}\b", command) is not None


def plan_step(job_name: str, index: int, step: dict, inherited: dict | None = None) -> dict:
    inherited = inherited or {}
    entry = _entry(
        f"{job_name}#{index}",
        job_name,
        name=step.get("name"),
        continue_on_error=bool(
            step.get("continue-on-error", inherited.get("continue_on_error", False))
        ),
        working_directory=step.get("working-directory") or inherited.get("working_directory"),
    )
    if "run" not in step:
        uses = step.get("uses") or "no run: block"
        return _skipped(entry, f"{uses} is a GitHub Action; only Actions can run it")

    command = run_text(step["run"])
    entry["command"] = command
    if not command:
        return _blocked(entry, "its `run:` value is empty, or is not a command at all")
    entry["kind"] = "test" if TEST_RUNNER.search(command) else "check"
    if PROVISIONING.search(command):
        return _skipped(
            entry, "installs rather than verifies; the gate does not rewrite your machine"
        )
    if "if" in step:
        return _blocked(entry, f"conditional on `{step['if']}`, which only Actions can evaluate")
    own = {str(k): _env_text(v) for k, v in (step.get("env") or {}).items()}
    if GITHUB_EXPRESSION.search(command) or any(GITHUB_EXPRESSION.search(v) for v in own.values()):
        return _blocked(entry, "carries a ${{ }} expression that only Actions can substitute")
    # An inherited value Actions would have substituted and this machine cannot:
    # harmless to a step that never reads it, and a silent wrong answer to one
    # that does.
    unset = [name for name in inherited.get("env_dropped", []) if _reads_variable(command, name)]
    if unset:
        return _blocked(
            entry,
            f"reads ${unset[0]}, which the workflow sets from a ${{{{ }}}} expression "
            "only Actions can substitute",
        )
    entry["env"] = {**inherited.get("env", {}), **own}
    if inherited.get("env_dropped"):
        entry["env_dropped"] = list(inherited["env_dropped"])
    return _runnable(entry)


def default_test_command(tests: list[str], changed: list[str]) -> str | None:
    """A runner, or nothing — never a guess.

    Only pytest is inferred, and only from the file extensions actually in play.
    Choosing between jest and vitest by vibe would produce a command that fails
    for reasons having nothing to do with the change, and a gate that cries wolf
    gets waived.
    """
    if any(str(path).endswith(".py") for path in (tests or changed)):
        return f"{shlex.quote(sys.executable)} -m pytest"
    return None


def plan_impact_step(
    tests: list[str],
    complete: bool,
    changed: list[str],
    test_command: str | None,
    ci_runs_tests: bool,
) -> dict:
    """The tests covering the diff, run first.

    First because it is the fastest way to learn the change is wrong, and a
    linter finding nothing in broken code is a slow way to learn nothing.
    """
    entry = _entry("impact", "impact", name="tests covering the diff", kind="test")
    if complete and not tests:
        return _skipped(entry, "no changed file maps to a test (documentation only)")
    command = test_command or default_test_command(tests, changed)
    if command is None:
        suffixes = sorted({Path(p).suffix for p in (tests or changed) if Path(p).suffix})
        return _skipped(
            entry,
            f"no test runner known for {', '.join(suffixes) or 'these files'}; pass --test-command",
        )
    if complete:
        # Naming the files IS the narrowing. Never narrow on a partial map: an
        # unmapped file is exactly where an unguarded regression hides.
        entry["command"] = " ".join([command, *(shlex.quote(t) for t in tests)])
    elif ci_runs_tests:
        return _skipped(entry, "the map is incomplete and a CI job below runs the whole suite")
    else:
        entry["command"] = command
    return _runnable(entry)


def build_plan(
    root: Path | str,
    changed: list[str],
    profile: dict | None = None,
    workflow_dir: Path | str | None = None,
    test_command: str | None = None,
) -> tuple[list[dict], dict]:
    """Everything the gate intends to do, decided before any of it is executed."""
    root = Path(root)
    workflow_dir = Path(workflow_dir) if workflow_dir else root / ".github" / "workflows"
    jobs, deferred, notes = select_jobs(profile, workflow_dir)

    steps: list[dict] = []
    for job in jobs:
        name = job["name"]
        doc, spec, problem = job_spec(workflow_dir, job)
        if problem:
            steps.append(_blocked(_entry(f"{name}#?", name), problem))
            continue
        # A job-level `if:` is evaluated by Actions before any step of the job
        # runs. The gate cannot evaluate it, and running the steps anyway is
        # how a job gated to `github.event_name == 'push'` — a publish, a
        # deploy — ran on a laptop. Same answer as a conditional step.
        if "if" in spec:
            steps.append(
                _blocked(
                    _entry(f"{name}#?", name),
                    f"the whole job is conditional on `{spec['if']}`, which only Actions "
                    "can evaluate",
                )
            )
            continue
        declared = [step for step in (spec.get("steps") or []) if isinstance(step, dict)]
        # A job with `uses:` and no steps calls a reusable workflow, and that is
        # where a repo's real suite often lives. It used to contribute nothing to
        # the plan at all, so a green could be reported having verified nothing
        # about it. Unrunnable, said out loud, like any other check this
        # machine cannot run.
        if not declared and spec.get("uses"):
            steps.append(
                _blocked(
                    _entry(f"{name}#?", name),
                    f"calls the reusable workflow {spec['uses']}, which the gate cannot run "
                    "here; CI will be the first to",
                )
            )
            continue
        inherited = job_context(doc, spec)
        for index, step in enumerate(declared, start=1):
            steps.append(plan_step(name, index, step, inherited))

    tests, complete = ci_profile.impacted_tests(changed, root)
    ci_runs_tests = any(s["action"] == "run" and s["kind"] == "test" for s in steps)
    steps.insert(0, plan_impact_step(tests, complete, changed, test_command, ci_runs_tests))

    if not any(step["action"] == "run" for step in steps):
        notes.append(
            "no command to run: the gate could not work out what CI does with this change, "
            "which is not the same as there being nothing to check."
        )
    context = {
        "root": str(root),
        "changed": list(changed),
        "impacted_tests": tests,
        "impact_complete": complete,
        # Documentation maps to no test by design; everything else has to be
        # covered by something that actually executes.
        "tests_required": not (complete and not tests),
        "jobs": [job["name"] for job in jobs],
        "deferred_to_ci": deferred,
        "notes": notes,
    }
    return steps, context


# --- execution ----------------------------------------------------------------


def run_command(command: str, cwd: Path, env: dict[str, str]) -> dict:
    """Run one step the way Actions would: bash, with `-e` and `-o pipefail`.

    The shell matters. Under plain `sh`, a multi-line `run:` block whose first
    line fails still exits on the status of its last line, so a red step reports
    green — the same silent pass this whole script is about.
    """
    bash = shutil.which("bash")
    argv = (
        [bash, "--noprofile", "--norc", "-e", "-o", "pipefail", "-c", command]
        if bash
        else ["sh", "-c", "set -e\n" + command]
    )
    started = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            env={**os.environ, **env},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        code, output = proc.returncode, proc.stdout or ""
    except OSError as exc:  # no shell at all: report it, never assume green
        code, output = 126, f"could not start a shell: {exc}"
    return {
        "status": "passed" if code == 0 else "failed",
        "exit_code": code,
        "duration_s": round(time.monotonic() - started, 2),
        "output": output[-OUTPUT_TAIL:],
    }


def execute(
    steps: list[dict],
    root: Path | str,
    keep_going: bool = False,
    progress=None,
    run_past_unrunnable: bool = False,
):
    """Run the plan. A failure stops everything after it; an unrunnable step stops
    the rest of its job.

    The second rule mirrors Actions: a step whose tool is missing exits non-zero
    there, and the job halts on it exactly as on any failure. Running the later
    steps of that job here reaches commands CI would never have reached — and
    the report has already said the job cannot be certified, so nothing those
    steps prove can be counted. `run_past_unrunnable` is for the caller that
    has decided to let CI be the first to run the missing check and wants to
    see everything else here anyway.
    """
    root = Path(root)
    stop = False
    halted: set[str] = set()
    for step in steps:
        if step["action"] == "skip":
            step["status"] = "skipped"
        elif step["action"] == "block":
            step["status"] = "unrunnable"
            if not run_past_unrunnable and not step["continue_on_error"]:
                halted.add(step["job"])
        elif stop:
            step["status"] = "not_run"
            step["reason"] = "an earlier step failed"
        elif step["job"] in halted:
            step["status"] = "not_run"
            step["reason"] = "an earlier step in this job could not run, and CI would stop there"
        else:
            cwd = root / (step.get("working_directory") or ".")
            step.update(run_command(step["command"], cwd, step.get("env") or {}))
            if step["status"] == "passed":
                step.pop("output", None)  # only a failure's output is worth keeping
            elif not keep_going and not step["continue_on_error"]:
                stop = True
        if progress:
            progress(step)
    return steps


def summarise(steps: list[dict], context: dict, allow_unrunnable: bool) -> dict:
    failed = [s for s in steps if s["status"] == "failed" and not s["continue_on_error"]]
    unrunnable = [s for s in steps if s["status"] == "unrunnable"]
    skipped = [s for s in steps if s["status"] == "skipped"]
    passed = [s for s in steps if s["status"] == "passed"]
    tests_ran = any(s["kind"] == "test" for s in passed)
    notes = list(context["notes"])

    # Order matters: a real failure outranks every other reason to be unhappy,
    # and no waiver may bury one.
    if failed:
        status, code = "failed", 1
    elif unrunnable:
        status, code = ("waived", 0) if allow_unrunnable else ("blocked", 2)
        if allow_unrunnable:
            notes.append(
                "waived: the checks under `unrunnable` did not run here, so CI will be the "
                "first thing to run them."
            )
    elif not passed:
        status, code = "blocked", 2
    elif context["tests_required"] and not tests_ran:
        status, code = "blocked", 2
        notes.append(
            "no test ran over changed code: the gate will not certify a change it never "
            "exercised. Pass --test-command, or map the changed files to tests."
        )
    else:
        status, code = "green", 0

    return {
        "status": status,
        "exit_code": code,
        **{k: v for k, v in context.items() if k != "notes"},
        "tests_ran": tests_ran,
        "steps": steps,
        "failed": [
            {
                "id": s["id"],
                "command": s["command"],
                "exit_code": s["exit_code"],
                "output": s["output"],
            }
            for s in failed
        ],
        "unrunnable": [
            {"id": s["id"], "command": s["command"], "reason": s["reason"]} for s in unrunnable
        ],
        "skipped": [{"id": s["id"], "reason": s["reason"]} for s in skipped],
        "notes": notes,
    }


def run_gate(
    root: Path | str = ".",
    changed: list[str] | None = None,
    profile: dict | None = None,
    workflow_dir: Path | str | None = None,
    test_command: str | None = None,
    allow_unrunnable: bool = False,
    keep_going: bool = False,
    progress=None,
) -> dict:
    steps, context = build_plan(root, changed or [], profile, workflow_dir, test_command)
    execute(steps, root, keep_going, progress, run_past_unrunnable=keep_going or allow_unrunnable)
    return summarise(steps, context, allow_unrunnable)


# --- the diff under test ------------------------------------------------------


def _git(root: Path, args: list[str]) -> str:
    """One git command's output, or the reason there is none.

    This used to swallow every error and return "", which made "git could not
    compute the diff" and "the diff is empty" the same value. They are opposite
    claims, and the empty one is the dangerous half: no changed file maps to no
    required test, so the gate reported green having run nothing at all.
    """
    try:
        done = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
        )
    except OSError as exc:
        raise PathsUnavailable(f"`git {' '.join(args)}` could not be run: {exc}") from exc
    except subprocess.CalledProcessError as exc:
        said = (exc.stderr or "").strip().splitlines()
        raise PathsUnavailable(
            f"`git {' '.join(args)}` exited {exc.returncode}: {said[0] if said else 'no output'}"
        ) from exc
    return done.stdout


def changed_files(root: Path, base: str) -> list[str]:
    """Everything this branch changed, committed or not.

    A gate reading only commits misses the edit made while reading its output,
    which is the edit most likely to be wrong.

    Raises PathsUnavailable when git cannot answer one of its three questions —
    an unknown base ref (the default `main` on a `master` trunk), a shallow clone
    without the base, no git on PATH. Returning [] there would be the gate
    deciding a change it could not see needs no test.
    """
    found: set[str] = set()
    for args in (
        ["diff", "--name-only", f"{base}...HEAD"],
        ["diff", "--name-only", "HEAD"],
        ["ls-files", "--others", "--exclude-standard"],
    ):
        found.update(line.strip() for line in _git(root, args).splitlines() if line.strip())
    return sorted(found)


def undeterminable_diff(base: str, exc: PathsUnavailable, root: Path | str = ".") -> dict:
    """The report for a gate that never learned what it was grading.

    `blocked`, the same status and exit 2 a check this machine cannot run gets,
    and for the same reason: an unknown is not a pass. Naming the way out matters
    as much as the refusal — the default base is `main`, so a repo whose trunk is
    `master` or `develop` hits this on the very first run, and a gate whose only
    output is "no" gets worked around rather than fixed.

    Same shape as every other report (issue #82). The exit-2 path is the one an
    automated caller is most likely to parse, because it is the one that needs
    explaining, and a report that differs only on the failure path is the one
    that breaks a consumer later. So the context keys are all here and say the
    truthful thing: no file is known to have changed, the impact map is not
    complete, tests are required and none ran.
    """
    return {
        "status": "blocked",
        "exit_code": 2,
        "root": str(root),
        "changed": [],
        "impacted_tests": [],
        "impact_complete": False,
        "tests_required": True,
        "jobs": [],
        "deferred_to_ci": [],
        "tests_ran": False,
        "steps": [],
        "failed": [],
        "unrunnable": [],
        "skipped": [],
        "notes": [
            f"the diff against `{base}` could not be read, so the gate does not know what "
            f"this change touches: {exc}",
            "it will not fall back to an empty diff. Nothing changed and nothing could be "
            "determined look identical from here, and the second one requires no test, "
            "which is how a gate that ran nothing reports green. Pass --base with this "
            "repo's trunk, or --changed to name the files yourself.",
        ],
    }


# --- CLI ----------------------------------------------------------------------

MARKS = {
    "passed": "ok",
    "failed": "FAILED",
    "skipped": "skip",
    "unrunnable": "COULD NOT RUN",
    "not_run": "not run",
}


def _progress_line(step: dict) -> str:
    # Reason before command: for anything that did not run, why it did not is the
    # whole message, and a line that reads like a command invites you to assume
    # it ran.
    detail = step.get("reason") or step.get("command") or ""
    head = detail.splitlines()[0] if detail else ""
    return f"gate: {MARKS[step['status']]:>13}  {step['id']:<10}  {head}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("run", "plan"):
        p = sub.add_parser(name)
        p.add_argument("--root", default=".")
        p.add_argument("--base", default="main", help="branch the diff is measured against")
        p.add_argument("--changed", nargs="+", help="override the diff (default: ask git)")
        p.add_argument(
            "--profile",
            help=f"CI profile (default {ledger.LEDGER_DIR}/{ledger.PROFILE_FILE} in the "
            "repository --root belongs to)",
        )
        p.add_argument("--workflows", default=".github/workflows")
        p.add_argument("--test-command", default=None, help="runner for the impacted tests")
        p.add_argument(
            "--allow-unrunnable",
            action="store_true",
            help="exit 0 despite checks this machine cannot run, naming every one",
        )
        p.add_argument("--keep-going", action="store_true", help="run past the first failure")

    args = parser.parse_args(argv)
    root = Path(args.root)

    def under_root(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else root / path

    # The workflows are tracked, so they sit under --root wherever that is. The
    # profile is not: it lives in the main checkout's `.foreman`, which a linked
    # worktree does not have, so it is anchored to the repository --root belongs
    # to rather than to --root itself (issue #74).
    profile = ledger.load_profile(args.profile, start=root) or None
    try:
        changed = args.changed if args.changed is not None else changed_files(root, args.base)
    except PathsUnavailable as exc:
        print(json.dumps(undeterminable_diff(args.base, exc, root), indent=2))
        return 2

    if args.cmd == "plan":
        steps, context = build_plan(
            root, changed, profile, under_root(args.workflows), args.test_command
        )
        print(json.dumps({"status": "planned", **context, "steps": steps}, indent=2))
        return 0

    report = run_gate(
        root=root,
        changed=changed,
        profile=profile,
        workflow_dir=under_root(args.workflows),
        test_command=args.test_command,
        allow_unrunnable=args.allow_unrunnable,
        keep_going=args.keep_going,
        progress=lambda step: print(_progress_line(step), file=sys.stderr, flush=True),
    )
    print(json.dumps(report, indent=2))
    return report["exit_code"]


if __name__ == "__main__":
    raise SystemExit(main())
