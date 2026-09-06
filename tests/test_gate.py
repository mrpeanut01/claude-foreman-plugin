"""The local gate: one command that either exits zero or names what failed.

Issue #47. The gate used to be prose — map the diff to its tests, run those,
then lint and typecheck, push when it is all green — and prose cannot be
executed. In a live run the operator ran the impact analysis and the tests, went
to push, and had never run lint at all. A lint step that was never run looks
exactly like a lint step that passed.

So these tests describe a gate that cannot be half-run. Every command CI would
run on this change is either executed here or reported as not executed, and
green requires that nothing was left unaccounted for.

They also own their PATH. The gate's entire subject is which commands this
machine can reach, so a test of it that inherits the developer's PATH is not
testing the gate, it is testing the laptop — and it passed on the laptop while
failing in CI, which is the same "an unrun check looked like a passing one"
this file exists to stop, aimed at itself. Every test here runs against a
toolbox holding a shell and whatever that test installed by name, so what is
available is an argument rather than an accident.
"""

import importlib
import json
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

# scripts/ is not an installed package: put it on the path, then load by name.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
gate = importlib.import_module("gate")


@pytest.fixture(autouse=True)
def toolbox(tmp_path, monkeypatch):
    """The only PATH any test in this file sees.

    A shell goes in because the gate cannot execute a single step without one —
    that is the substrate, not a check under test. Nothing else does: `ruff`,
    `uvx`, `touch` and the rest are installed by the tests that mean to have
    them, so that "the tool is missing" and "the tool is there" are two states a
    test chooses between, on every machine, rather than two machines.
    """
    directory = tmp_path / "toolbox"
    directory.mkdir()
    for shell in ("bash", "sh"):
        found = shutil.which(shell)
        if found:
            (directory / shell).symlink_to(found)
    monkeypatch.setenv("PATH", str(directory))
    return directory


def install_tool(toolbox, name, script=":\n"):
    """Put an executable of that name where the gate will find it, for one test."""
    path = toolbox / name
    path.write_text(f"#!/bin/sh\n{script}")
    path.chmod(0o755)
    return path


def install_marker_tool(toolbox, name="touch"):
    """A program that records having run, so "it never ran" is an observation."""
    return install_tool(toolbox, name, ': > "$1"\n')


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src").mkdir()
    return tmp_path


def workflow(repo, body, name="ci.yml"):
    (repo / ".github" / "workflows" / name).write_text(textwrap.dedent(body))


def profile(**jobs):
    """A profile shaped the way ci_profile.py writes one."""
    return {
        "jobs": {
            name: {
                "workflow_file": spec.get("workflow_file", "ci.yml"),
                "tier": spec.get("tier", "cheap"),
                "events": spec.get("events", {"pull_request": {}}),
            }
            for name, spec in jobs.items()
        }
    }


def run(repo, **kwargs):
    """Run the gate over a scratch repo. Documentation-only diff unless told otherwise."""
    kwargs.setdefault("changed", ["README.md"])
    kwargs.setdefault("test_command", "true")
    return gate.run_gate(root=repo, **kwargs)


def ids(entries):
    return [entry["id"] for entry in entries]


def only_python3(name):
    """A machine with python3 and no bare python — the common laptop."""
    return None if name == "python" else "/bin/" + name


# `run: true` is a step that passes and needs nothing installed — `true` is a
# shell builtin. It is also, unquoted, a YAML *boolean*, which is why the
# fixtures below leave it that way: Actions reads `run:` as a string and runs
# the two characters the author wrote, and a gate that stringifies PyYAML's bool
# into `True` instead looks for a program no machine has. See
# `test_a_yaml_boolean_run_step_is_the_command_the_author_wrote`.
CHECK_ONLY = """
    name: CI
    on: [pull_request]
    jobs:
      lint:
        steps:
          - run: true
"""


# --- the whole point: a green means every check ran ---------------------------


def test_a_green_gate_ran_every_step_it_found(repo):
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: true
              - run: true
        """,
    )
    report = run(repo, profile=profile(lint={}))
    assert report["status"] == "green"
    assert report["exit_code"] == 0
    assert [s["status"] for s in report["steps"] if s["job"] == "lint"] == ["passed", "passed"]


def test_a_failing_step_fails_the_gate_and_names_the_command_that_failed(repo):
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - name: ruff
                run: exit 3
        """,
    )
    report = run(repo, profile=profile(lint={}))
    assert report["status"] == "failed"
    assert report["exit_code"] == 1
    (failure,) = report["failed"]
    assert failure["command"] == "exit 3"
    assert failure["exit_code"] == 3


def test_the_second_check_still_runs_when_the_first_one_passes(repo):
    """The #47 shape exactly: `ruff check` passing must not stand in for `ruff format`."""
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: true
              - run: exit 1
        """,
    )
    report = run(repo, profile=profile(lint={}))
    assert report["status"] == "failed"
    assert ids(report["failed"]) == ["lint#2"]


def test_the_gate_stops_at_the_first_red_rather_than_burning_the_whole_suite(repo):
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: exit 1
              - run: true
        """,
    )
    report = run(repo, profile=profile(lint={}))
    assert [s["status"] for s in report["steps"] if s["job"] == "lint"] == ["failed", "not_run"]


def test_keep_going_reports_every_failure_in_one_pass(repo):
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: exit 1
              - run: exit 2
        """,
    )
    report = run(repo, profile=profile(lint={}), keep_going=True)
    assert ids(report["failed"]) == ["lint#1", "lint#2"]


def test_the_captured_output_of_a_failing_step_comes_back_with_the_report(repo):
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: echo 'E501 line too long'; exit 1
        """,
    )
    report = run(repo, profile=profile(lint={}))
    assert "E501 line too long" in report["failed"][0]["output"]


def test_a_multiline_step_fails_when_any_line_fails_not_just_the_last(repo):
    """GitHub runs a `run:` block under `bash -e`. A gate using plain `sh` would
    let a failing first line hide behind a passing last one."""
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: |
                  exit 1
                  true
        """,
    )
    assert run(repo, profile=profile(lint={}))["status"] == "failed"


# --- a tool that is not installed ---------------------------------------------


def test_a_missing_tool_blocks_the_gate_instead_of_passing_it(repo):
    """The regression #47 is about: `ruff` unreachable used to read as green."""
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: foreman-no-such-linter check .
        """,
    )
    report = run(repo, profile=profile(lint={}))
    assert report["status"] == "blocked"
    assert report["exit_code"] == 2
    (blocked,) = report["unrunnable"]
    assert blocked["id"] == "lint#1"
    assert "foreman-no-such-linter" in blocked["reason"]


def test_a_missing_tool_is_reported_as_unrunnable_and_not_as_a_failure(repo):
    """A machine that lacks ruff and code that fails ruff call for different acts."""
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: foreman-no-such-linter check .
        """,
    )
    report = run(repo, profile=profile(lint={}))
    assert report["failed"] == []
    assert ids(report["unrunnable"]) == ["lint#1"]


def test_a_step_whose_tool_is_missing_is_never_executed(repo, toolbox):
    install_marker_tool(toolbox)  # so only the gate's refusal keeps the file away
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: foreman-no-such-linter && touch ran-anyway
        """,
    )
    run(repo, profile=profile(lint={}))
    assert not (repo / "ran-anyway").exists()


def test_allowing_unrunnable_steps_exits_zero_but_still_names_what_did_not_run(repo):
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: foreman-no-such-linter check .
        """,
    )
    report = run(repo, profile=profile(lint={}), allow_unrunnable=True)
    assert report["status"] == "waived"
    assert report["exit_code"] == 0
    assert ids(report["unrunnable"]) == ["lint#1"]


def test_a_waiver_does_not_hide_a_real_failure(repo):
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: foreman-no-such-linter check .
              - run: exit 1
        """,
    )
    report = run(repo, profile=profile(lint={}), allow_unrunnable=True, keep_going=True)
    assert report["status"] == "failed"
    assert report["exit_code"] == 1


LINT_STEP = """
    name: CI
    on: [pull_request]
    jobs:
      lint:
        steps:
          - run: foreman-linter check .
"""


def test_installing_the_tool_is_the_only_difference_between_blocked_and_green(repo, toolbox):
    """Both outcomes, one machine, one command.

    This is the assertion the suite was missing. It used to test the first half
    against a name nothing could have (`foreman-no-such-linter`) and the second
    half against whatever the developer happened to have installed — which is
    how it came to pass on a laptop with ruff and fail in CI without one. Here
    the only thing that moves is whether the program exists.
    """
    workflow(repo, LINT_STEP)
    assert run(repo, profile=profile(lint={}))["status"] == "blocked"

    install_tool(toolbox, "foreman-linter")
    assert run(repo, profile=profile(lint={}))["status"] == "green"


def test_a_tool_the_machine_has_but_the_test_did_not_install_is_out_of_reach(repo):
    """The CI condition, written down.

    CI installs pytest and pyyaml and nothing else, so `uvx` is not there. A
    developer's machine has it. If that difference can reach these tests, they
    are measuring the machine.
    """
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: uvx ruff check .
        """,
    )
    report = run(repo, profile=profile(lint={}))
    assert report["status"] == "blocked"
    assert report["unrunnable"][0]["reason"] == "`uvx` is not on PATH"


def test_an_executable_that_is_absent_from_path_counts_as_missing():
    assert gate.missing_requirements("foreman-no-such-linter check .")
    assert gate.missing_requirements("true && foreman-no-such-linter")
    assert gate.missing_requirements("true | foreman-no-such-linter")
    assert not gate.missing_requirements("true && true")


def test_a_module_the_interpreter_cannot_import_counts_as_missing_too():
    """`python -m ruff` where ruff is only reachable through uvx exits non-zero
    having checked nothing. That is how #47's two lint errors survived."""
    assert gate.missing_requirements(f"{sys.executable} -m foreman_no_such_module .")
    assert not gate.missing_requirements(f"{sys.executable} -m json.tool --help")


def test_shell_builtins_are_not_mistaken_for_missing_programs():
    assert not gate.missing_requirements("cd src && true")
    assert not gate.missing_requirements("FOO=bar true")


def test_a_python_step_runs_under_python3_when_that_is_the_only_python(monkeypatch):
    """setup-python puts a bare `python` on the runner. Laptops often have only
    `python3`, and a gate that blocks on that for every Python repo is a gate
    nobody runs."""
    monkeypatch.setattr(gate.shutil, "which", only_python3)
    adapted, note = gate.adapt_command("python -m pytest tests/ -q")
    assert adapted == "python3 -m pytest tests/ -q"
    assert note


def test_the_python_alias_leaves_every_other_word_alone(monkeypatch):
    monkeypatch.setattr(gate.shutil, "which", only_python3)
    adapted, note = gate.adapt_command("uses-python3 --python-version=3.11")
    assert adapted == "uses-python3 --python-version=3.11"
    assert note is None


# --- what a `run:` value actually says ----------------------------------------


def test_a_yaml_boolean_run_step_is_the_command_the_author_wrote(repo):
    """`run: true` is the shell command `true`, not a program called `True`.

    Unquoted, YAML resolves it to a boolean, because YAML does not know Actions
    types `run:` as a string. Stringifying that bool the Python way yields
    `True` — a name no POSIX machine has — so the gate declared an ordinary
    no-op step unrunnable and refused to go green. It hid on macOS, where the
    filesystem is case-insensitive and `which("True")` cheerfully answers
    /usr/bin/true, and only showed up on Linux.
    """
    workflow(repo, CHECK_ONLY)
    report = run(repo, profile=profile(lint={}))
    assert report["status"] == "green"
    assert [s["command"] for s in report["steps"] if s["job"] == "lint"] == ["true"]


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, "true"),  # `run: true`, and `run: on`, and `run: yes`
        (False, "false"),  # `run: false`, and `run: off`, and `run: no`
        ("  ruff check .  ", "ruff check ."),
        (3, "3"),
    ],
)
def test_a_run_value_renders_as_the_scalar_the_workflow_holds(value, expected):
    assert gate.run_text(value) == expected


@pytest.mark.parametrize("value", [None, ["ruff", "check"], {"shell": "bash"}])
def test_a_run_value_that_is_no_command_at_all_renders_as_nothing(value):
    """Not every `run:` denotes a command, and the ones that do not must not be
    invented into one — a made-up command name reports a missing tool nobody
    ever asked for, which reads exactly like a real one."""
    assert gate.run_text(value) == ""


def test_a_step_whose_run_block_is_empty_blocks_rather_than_passing(repo):
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run:
        """,
    )
    report = run(repo, profile=profile(lint={}))
    assert report["status"] == "blocked"
    assert "run:" in report["unrunnable"][0]["reason"]


# --- steps that verify nothing ------------------------------------------------


def test_an_action_step_is_skipped_because_only_github_can_run_it(repo):
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - uses: actions/checkout@v4
              - run: true
        """,
    )
    report = run(repo, profile=profile(lint={}))
    assert report["status"] == "green"
    (skipped,) = [s for s in report["skipped"] if s["id"] == "lint#1"]
    assert "actions/checkout@v4" in skipped["reason"]


def test_an_install_step_is_never_run_against_the_developers_machine(repo):
    """A gate may not rewrite the machine it is grading. Skipping an install
    cannot turn red into green — it verifies nothing — and if it leaves a tool
    absent, that surfaces as a missing tool rather than as a pass."""
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          test:
            steps:
              - run: python -m pip install --upgrade pip pytest
              - run: true
        """,
    )
    report = run(repo, profile=profile(test={}))
    assert report["status"] == "green"
    (skipped,) = [s for s in report["skipped"] if s["id"] == "test#1"]
    assert "installs" in skipped["reason"]


@pytest.mark.parametrize(
    "command",
    [
        "pip install -r requirements.txt",
        "python -m pip install --upgrade pip",
        "uv sync --all-extras",
        "npm ci",
        "yarn install --frozen-lockfile",
        "poetry install",
        "go mod download",
        "sudo apt-get install -y libpq-dev",
    ],
)
def test_recognises_the_common_ways_to_provision_a_runner(command):
    assert gate.PROVISIONING.search(command), command


@pytest.mark.parametrize("command", ["ruff check .", "pytest -q", "npm run build", "make lint"])
def test_does_not_mistake_a_verification_step_for_provisioning(command):
    assert not gate.PROVISIONING.search(command), command


def test_a_step_carrying_a_github_expression_cannot_be_run_here(repo):
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          test:
            steps:
              - run: true --python ${{ matrix.python-version }}
        """,
    )
    report = run(repo, profile=profile(test={}))
    assert report["status"] == "blocked"
    assert "${{" in report["unrunnable"][0]["reason"]


def test_a_conditional_step_blocks_because_its_condition_belongs_to_actions(repo):
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - if: github.event_name == 'push'
                run: true
        """,
    )
    report = run(repo, profile=profile(lint={}))
    assert ids(report["unrunnable"]) == ["lint#1"]


def test_a_step_marked_continue_on_error_does_not_fail_the_gate(repo):
    """CI does not go red on it, so neither may the gate — matching CI is the job."""
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - continue-on-error: true
                run: exit 1
              - run: true
        """,
    )
    report = run(repo, profile=profile(lint={}))
    assert report["status"] == "green"
    assert report["failed"] == []


def test_a_step_runs_in_the_working_directory_it_declares(repo):
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - working-directory: src
                run: 'test "${PWD##*/}" = src'
        """,
    )
    assert run(repo, profile=profile(lint={}))["status"] == "green"


# --- which jobs the gate is entitled to run -----------------------------------


def test_a_job_a_pull_request_does_not_trigger_is_never_run_locally(repo, toolbox):
    """A cheap push-only job can be a deploy. Running one on a laptop is not a
    gate, it is an accident."""
    install_marker_tool(toolbox)
    workflow(
        repo,
        """
        name: CI
        on: [push, pull_request]
        jobs:
          lint:
            steps:
              - run: true
          deploy:
            steps:
              - run: touch deployed
        """,
    )
    report = run(
        repo,
        profile=profile(lint={}, deploy={"events": {"push": {"branches": ["main"]}}}),
    )
    assert not (repo / "deployed").exists()
    assert {s["job"] for s in report["steps"]} == {"impact", "lint"}


def test_an_expensive_job_is_left_to_ci_and_named_in_the_report(repo, toolbox):
    install_marker_tool(toolbox)
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: true
          e2e:
            steps:
              - run: touch ran-e2e
        """,
    )
    report = run(repo, profile=profile(lint={}, e2e={"tier": "expensive"}))
    assert not (repo / "ran-e2e").exists()
    assert [d["job"] for d in report["deferred_to_ci"]] == ["e2e"]


def test_an_untiered_job_is_included_rather_than_silently_dropped(repo):
    """No cost data is not evidence a job is expensive, and dropping a check
    because nobody measured it is the failure this whole script is about."""
    workflow(repo, CHECK_ONLY)
    report = run(repo, profile=profile(lint={"tier": "unmeasured"}))
    assert [s["id"] for s in report["steps"] if s["job"] == "lint"] == ["lint#1"]
    assert any("unmeasured" in note for note in report["notes"])


def test_an_older_profile_that_lists_triggers_instead_of_events_still_works(repo):
    """Profiles on disk outlive the schema that wrote them."""
    workflow(repo, CHECK_ONLY)
    legacy = {
        "jobs": {"lint": {"workflow_file": "ci.yml", "tier": "cheap", "triggers": ["pull_request"]}}
    }
    assert run(repo, profile=legacy)["status"] == "green"


def test_without_a_profile_the_gate_reads_the_workflows_and_says_it_did(repo):
    workflow(repo, CHECK_ONLY)
    report = run(repo, profile=None)
    assert report["status"] == "green"
    assert any("probe" in note for note in report["notes"])


def test_a_profile_naming_a_job_the_workflow_no_longer_has_is_blocked(repo):
    """Not knowing the commands is not the same as there being none to run."""
    workflow(repo, CHECK_ONLY)
    report = run(repo, profile=profile(lint={}, typecheck={}))
    assert report["status"] == "blocked"
    assert "typecheck" in report["unrunnable"][0]["reason"]


def test_a_profile_pointing_at_a_workflow_file_that_is_gone_is_blocked(repo):
    workflow(repo, CHECK_ONLY)
    report = run(repo, profile=profile(lint={"workflow_file": "removed.yml"}))
    assert report["status"] == "blocked"
    assert "removed.yml" in report["unrunnable"][0]["reason"]


def test_finding_no_commands_at_all_is_blocked_rather_than_green(repo):
    """Silence is the one result a gate may never report as success."""
    workflow(
        repo,
        """
        name: Nightly
        on:
          schedule: [{cron: '0 3 * * *'}]
        jobs:
          soak:
            steps:
              - run: true
        """,
    )
    report = run(repo, profile=None)
    assert report["status"] == "blocked"
    assert any("no command" in note for note in report["notes"])


# --- the tests that cover the diff --------------------------------------------


def test_the_tests_covering_the_diff_run_before_anything_else(repo):
    """Fastest signal first: a broken change should not wait behind a lint run."""
    workflow(repo, CHECK_ONLY)
    (repo / "tests" / "test_upload.py").write_text("")
    report = run(repo, profile=profile(lint={}), changed=["src/upload.py"])
    first = report["steps"][0]
    assert first["job"] == "impact"
    assert "tests/test_upload.py" in first["command"]
    assert report["impacted_tests"] == ["tests/test_upload.py"]


def test_an_unmapped_file_widens_the_run_to_the_whole_suite(repo):
    workflow(repo, CHECK_ONLY)
    report = run(repo, profile=profile(lint={}), changed=["src/upload.py"])
    assert report["impact_complete"] is False
    assert report["steps"][0]["command"] == "true"


def test_the_narrowed_run_is_skipped_when_a_ci_job_runs_the_suite_unnarrowed(repo):
    """Running the same untargeted suite twice is the waste the gate exists to save."""
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          test:
            steps:
              - run: true -m pytest tests/ -q
        """,
    )
    report = run(repo, profile=profile(test={}), changed=["src/upload.py"])
    assert report["steps"][0]["status"] == "skipped"
    assert report["status"] == "green"


def test_a_failing_impacted_test_stops_the_gate_before_the_linters(repo):
    workflow(repo, CHECK_ONLY)
    (repo / "tests" / "test_upload.py").write_text("")
    report = run(repo, profile=profile(lint={}), changed=["src/upload.py"], test_command="false")
    assert report["status"] == "failed"
    assert ids(report["failed"]) == ["impact"]
    assert [s["status"] for s in report["steps"] if s["job"] == "lint"] == ["not_run"]


def test_the_gate_refuses_to_report_green_when_no_test_ran_over_changed_code(repo):
    workflow(repo, CHECK_ONLY)
    report = run(repo, profile=profile(lint={}), changed=["src/app.rb"], test_command=None)
    assert report["status"] == "blocked"
    assert report["tests_ran"] is False
    assert any("no test" in note for note in report["notes"])


def test_a_documentation_only_change_needs_no_test_step(repo):
    workflow(repo, CHECK_ONLY)
    report = run(repo, profile=profile(lint={}), changed=["README.md"], test_command=None)
    assert report["status"] == "green"
    assert report["tests_ran"] is False


def test_a_python_diff_defaults_to_pytest_without_being_told(repo):
    workflow(repo, CHECK_ONLY)
    (repo / "tests" / "test_upload.py").write_text("")
    steps, _ = gate.build_plan(root=repo, changed=["src/upload.py"], profile=profile(lint={}))
    assert "-m pytest" in steps[0]["command"]


def test_a_language_with_no_known_runner_says_so_instead_of_guessing(repo):
    workflow(repo, CHECK_ONLY)
    steps, _ = gate.build_plan(root=repo, changed=["src/app.rb"], profile=profile(lint={}))
    assert steps[0]["action"] == "skip"
    assert "--test-command" in steps[0]["reason"]


# --- the diff the gate is grading ---------------------------------------------
# Everything above takes the changed set as given. Working it out is the step
# before, and it has the same failure mode one level up: a diff git could not
# compute came back as a diff with nothing in it, nothing maps to a test, so no
# test was required and the whole gate reported green having run none.

QUIET_GIT = "exit 0\n"
NAMING_GIT = "echo src/upload.py\n"
BROKEN_GIT = "echo 'fatal: bad revision' >&2\nexit 128\n"


def test_a_branch_that_changed_nothing_is_an_answer(repo, toolbox):
    """The half that must keep working: git said nothing changed, and meant it."""
    install_tool(toolbox, "git", QUIET_GIT)
    assert gate.changed_files(repo, "main") == []


def test_what_git_names_is_what_the_gate_grades(repo, toolbox):
    install_tool(toolbox, "git", NAMING_GIT)
    assert gate.changed_files(repo, "main") == ["src/upload.py"]


def test_a_diff_git_could_not_compute_is_not_a_diff_with_nothing_in_it(repo, toolbox):
    """`--base` defaults to main, so every master/develop trunk lands here."""
    install_tool(toolbox, "git", BROKEN_GIT)
    with pytest.raises(gate.PathsUnavailable):
        gate.changed_files(repo, "nosuchbase")


def test_git_missing_altogether_is_not_a_diff_with_nothing_in_it_either(repo):
    with pytest.raises(gate.PathsUnavailable):
        gate.changed_files(repo, "main")


def test_a_diff_the_gate_could_not_read_blocks_it_rather_than_passing_it(repo, toolbox, capsys):
    install_tool(toolbox, "git", BROKEN_GIT)
    workflow(repo, CHECK_ONLY)
    code = gate.main(["run", "--root", str(repo), "--profile", "none.json"])
    report = out(capsys)
    assert code == 2
    assert report["status"] == "blocked"
    assert report["tests_ran"] is False
    assert any("--base" in note for note in report["notes"])


def test_plan_will_not_plan_against_a_diff_it_could_not_read(repo, toolbox, capsys):
    install_tool(toolbox, "git", BROKEN_GIT)
    workflow(repo, CHECK_ONLY)
    code = gate.main(["plan", "--root", str(repo), "--profile", "none.json"])
    assert code == 2
    assert out(capsys)["status"] == "blocked"


def test_naming_the_changed_files_never_asks_git_at_all(repo, toolbox, capsys):
    """The way out: git being unreachable must not make the gate unusable."""
    install_tool(toolbox, "git", BROKEN_GIT)
    workflow(repo, CHECK_ONLY)
    code = gate.main(
        [
            "run",
            "--root",
            str(repo),
            "--changed",
            "README.md",
            "--profile",
            "none.json",
            "--test-command",
            "true",
        ]
    )
    assert code == 0
    assert out(capsys)["status"] == "green"


# --- the command line ---------------------------------------------------------


def out(capsys):
    return json.loads(capsys.readouterr().out)


def test_plan_says_what_would_run_without_running_any_of_it(repo, toolbox, capsys):
    install_marker_tool(toolbox)  # the step is runnable; plan still must not run it
    workflow(
        repo,
        """
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: touch ran
        """,
    )
    code = gate.main(
        ["plan", "--root", str(repo), "--changed", "README.md", "--profile", "none.json"]
    )
    plan = out(capsys)
    assert code == 0
    assert not (repo / "ran").exists()
    assert [s["command"] for s in plan["steps"] if s["job"] == "lint"] == ["touch ran"]


@pytest.mark.parametrize(
    "step,expected", [("true", 0), ("exit 1", 1), ("foreman-no-such-linter", 2)]
)
def test_the_exit_code_separates_green_from_red_from_could_not_run(repo, capsys, step, expected):
    workflow(
        repo,
        f"""
        name: CI
        on: [pull_request]
        jobs:
          lint:
            steps:
              - run: {step}
        """,
    )
    code = gate.main(
        [
            "run",
            "--root",
            str(repo),
            "--changed",
            "README.md",
            "--profile",
            "none.json",
            "--test-command",
            "true",
        ]
    )
    assert code == expected
    assert out(capsys)["exit_code"] == expected
