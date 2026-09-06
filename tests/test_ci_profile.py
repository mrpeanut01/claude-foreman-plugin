"""CI profile: learn the repo's job graph, costs, flake rates, and test mapping."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ci_profile  # noqa: E402
import globs  # noqa: E402
import ledger  # noqa: E402

# --- workflow parsing ---------------------------------------------------------


@pytest.fixture
def workflows(tmp_path):
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        textwrap.dedent("""
        name: CI
        on:
          pull_request:
            paths: ['src/**', 'tests/**']
        jobs:
          lint:
            runs-on: ubuntu-latest
            steps: [{run: ruff check}]
          unit:
            needs: [lint]
            steps: [{run: pytest tests/unit}]
          integration:
            needs: [unit]
            steps: [{run: pytest tests/integration}]
    """)
    )
    (d / "nightly.yml").write_text(
        textwrap.dedent("""
        name: Nightly
        on:
          schedule: [{cron: '0 3 * * *'}]
        jobs:
          soak:
            steps: [{run: make soak}]
    """)
    )
    return d


def test_parses_jobs_across_workflows(workflows):
    jobs = ci_profile.parse_workflows(workflows)
    assert {j["name"] for j in jobs} == {"lint", "unit", "integration", "soak"}


def test_records_job_dependencies(workflows):
    jobs = {j["name"]: j for j in ci_profile.parse_workflows(workflows)}
    assert jobs["lint"]["needs"] == []
    assert jobs["integration"]["needs"] == ["unit"]


def test_records_triggers_and_path_filters(workflows):
    jobs = {j["name"]: j for j in ci_profile.parse_workflows(workflows)}
    assert jobs["unit"]["triggers"] == ["pull_request"]
    assert jobs["unit"]["path_filters"] == ["src/**", "tests/**"]
    assert jobs["soak"]["triggers"] == ["schedule"]
    assert jobs["soak"]["path_filters"] == []


def test_malformed_workflow_is_skipped_with_a_warning(workflows):
    (workflows / "broken.yml").write_text("name: [unclosed\n")
    jobs, problems = ci_profile.parse_workflows(workflows, report_problems=True)
    assert {j["name"] for j in jobs} == {"lint", "unit", "integration", "soak"}
    assert any("broken.yml" in p for p in problems)


# --- durations ----------------------------------------------------------------


def _job(name, sha, conclusion, seconds, run_id=1):
    start = "2026-09-01T10:00:00Z"
    end_min, end_sec = divmod(seconds, 60)
    return {
        "run_id": run_id,
        "head_sha": sha,
        "name": name,
        "conclusion": conclusion,
        "started_at": start,
        "completed_at": f"2026-09-01T10:{end_min:02d}:{end_sec:02d}Z",
    }


def test_duration_stats_use_nearest_rank_percentiles():
    jobs = [
        _job("unit", f"sha{i}", "success", s, run_id=i)
        for i, s in enumerate([60, 90, 120, 150, 600])
    ]
    stats = ci_profile.duration_stats(jobs)["unit"]
    assert stats["n"] == 5
    assert stats["p50"] == 120
    assert stats["p95"] == 600


def test_duration_stats_ignore_cancelled_and_skipped_jobs():
    jobs = [
        _job("unit", "a", "success", 100, 1),
        _job("unit", "b", "cancelled", 5, 2),
        _job("unit", "c", "skipped", 1, 3),
    ]
    assert ci_profile.duration_stats(jobs)["unit"]["n"] == 1


# --- tiers --------------------------------------------------------------------


def test_tiers_split_on_the_configured_threshold():
    stats = {"lint": {"p95": 45}, "unit": {"p95": 240}, "integration": {"p95": 2400}}
    tiers = ci_profile.classify_tiers(stats, threshold_s=300)
    assert tiers == {"lint": "cheap", "unit": "cheap", "integration": "expensive"}


# --- flake rate ---------------------------------------------------------------


def test_flake_is_a_job_that_both_failed_and_passed_on_one_sha():
    jobs = [
        _job("integration", "sha1", "failure", 100, 1),
        _job("integration", "sha1", "success", 100, 2),
        _job("integration", "sha2", "success", 100, 3),
        _job("integration", "sha3", "success", 100, 4),
        _job("integration", "sha4", "success", 100, 5),
    ]
    assert ci_profile.flake_rates(jobs)["integration"] == pytest.approx(0.25)


def test_a_sha_that_only_ever_failed_is_a_real_failure_not_a_flake():
    jobs = [_job("unit", "sha1", "failure", 10, 1), _job("unit", "sha1", "failure", 10, 2)]
    assert ci_profile.flake_rates(jobs)["unit"] == 0.0


# --- required checks ----------------------------------------------------------


def test_required_checks_read_from_branch_protection():
    protection = {
        "required_status_checks": {
            "contexts": ["lint", "unit"],
            "checks": [{"context": "integration"}],
        }
    }
    assert ci_profile.required_checks(protection) == ["integration", "lint", "unit"]


def test_missing_protection_yields_no_required_checks():
    assert ci_profile.required_checks({}) == []
    assert ci_profile.required_checks(None) == []


# --- test impact --------------------------------------------------------------


@pytest.fixture
def repo(tmp_path):
    for p in [
        "src/foreman/ledger.py",
        "src/foreman/api.py",
        "tests/test_ledger.py",
        "tests/integration/test_api.py",
    ]:
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()
    return tmp_path


def test_changed_source_maps_to_its_test_by_convention(repo):
    hit, complete = ci_profile.impacted_tests(["src/foreman/ledger.py"], repo)
    assert hit == ["tests/test_ledger.py"]
    assert complete is True


def test_a_changed_test_file_maps_to_itself(repo):
    hit, complete = ci_profile.impacted_tests(["tests/integration/test_api.py"], repo)
    assert hit == ["tests/integration/test_api.py"]
    assert complete is True


def test_unmappable_change_forces_the_full_suite(repo):
    hit, complete = ci_profile.impacted_tests(["Dockerfile"], repo)
    assert complete is False, "an unmapped file must not silently narrow the suite"


def test_partial_mapping_still_forces_the_full_suite(repo):
    hit, complete = ci_profile.impacted_tests(["src/foreman/ledger.py", "Dockerfile"], repo)
    assert "tests/test_ledger.py" in hit
    assert complete is False


def test_docs_only_change_maps_to_no_tests_and_stays_complete(repo):
    hit, complete = ci_profile.impacted_tests(["README.md", "docs/guide.md"], repo)
    assert hit == []
    assert complete is True


# --- assembly -----------------------------------------------------------------


def test_build_profile_assembles_every_part(workflows, repo):
    jobs_json = [_job("unit", "a", "success", 100, 1), _job("integration", "a", "success", 900, 2)]
    profile = ci_profile.build_profile(
        workflow_dir=workflows,
        job_runs=jobs_json,
        protection={"required_status_checks": {"contexts": ["unit"]}},
        threshold_s=300,
    )
    assert profile["jobs"]["integration"]["tier"] == "expensive"
    assert profile["jobs"]["unit"]["tier"] == "cheap"
    assert profile["jobs"]["unit"]["required"] is True
    assert profile["jobs"]["integration"]["required"] is False
    assert profile["jobs"]["unit"]["needs"] == ["lint"]
    assert profile["cheap_tier_s"] == 100 and profile["expensive_tier_s"] == 900
    assert "generated_at" in profile


def test_job_with_no_observed_runs_is_reported_as_unmeasured(workflows):
    profile = ci_profile.build_profile(
        workflow_dir=workflows, job_runs=[], protection={}, threshold_s=300
    )
    assert profile["jobs"]["soak"]["tier"] == "unmeasured"
    assert profile["unmeasured_jobs"] == ["integration", "lint", "soak", "unit"]


@pytest.fixture
def matrix_workflow(tmp_path):
    """A matrix job plus a plain one, both reported under names of their own."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        textwrap.dedent("""
        name: CI
        on: [pull_request]
        jobs:
          test:
            strategy:
              matrix:
                python-version: ['3.11', '3.12']
            steps: [{run: pytest}]
          docs:
            steps: [{run: make docs}]
    """)
    )
    return d


def test_a_matrix_job_whose_cells_are_required_is_recorded_as_required(matrix_workflow):
    """Protection names the cells GitHub reports, never the job key they came from."""
    profile = ci_profile.build_profile(
        workflow_dir=matrix_workflow,
        job_runs=[],
        protection={"required_status_checks": {"contexts": ["test (3.11)", "test (3.12)"]}},
    )
    assert profile["jobs"]["test"]["required"] is True
    assert profile["jobs"]["docs"]["required"] is False


def test_a_matrix_job_with_only_some_cells_required_still_counts_as_required(matrix_workflow):
    """One required cell is enough to block a merge, so the job can block one."""
    profile = ci_profile.build_profile(
        workflow_dir=matrix_workflow,
        job_runs=[],
        protection={"required_status_checks": {"contexts": ["test (3.11)"]}},
    )
    assert profile["jobs"]["test"]["required"] is True
    # The per-cell truth is not lost by the flag rounding up: the exact contexts
    # protection named are still on the profile for anyone who needs them.
    assert profile["required_checks"] == ["test (3.11)"]


def test_a_required_context_no_workflow_declares_marks_no_job_required(matrix_workflow):
    """A third-party gate resolves to no job rather than to a plausible-looking one."""
    profile = ci_profile.build_profile(
        workflow_dir=matrix_workflow,
        job_runs=[],
        protection={"required_status_checks": {"contexts": ["codecov/patch"]}},
    )
    assert [n for n, j in profile["jobs"].items() if j["required"]] == []


# --- probe guards -------------------------------------------------------------


def test_probe_refuses_to_mix_a_remote_repo_with_local_workflows(monkeypatch, tmp_path):
    """Run history from one repo plus workflow files from another is nonsense."""
    monkeypatch.setattr(ci_profile, "current_repo", lambda: "me/mine")
    with pytest.raises(ci_profile.ProfileError) as exc:
        ci_profile.probe("someone/else", workflow_dir=None)
    assert "someone/else" in str(exc.value) and "me/mine" in str(exc.value)


def test_probe_allows_a_remote_repo_when_workflows_are_named_explicitly(monkeypatch, workflows):
    monkeypatch.setattr(ci_profile, "current_repo", lambda: "me/mine")
    monkeypatch.setattr(ci_profile, "_fetch_job_runs", lambda repo, runs, branch: [])
    monkeypatch.setattr(ci_profile, "_fetch_protection", lambda repo: {})
    profile = ci_profile.probe("someone/else", workflow_dir=workflows)
    assert profile["repo"] == "someone/else"
    assert "lint" in profile["jobs"]


def test_missing_workflow_directory_is_a_clear_error(tmp_path):
    with pytest.raises(ci_profile.ProfileError) as exc:
        ci_profile.probe("me/mine", workflow_dir=tmp_path / "nope")
    assert "nope" in str(exc.value)


# --- attributing observed runs to declared jobs -------------------------------
# GitHub reports a job's *display* name, with matrix values appended. Workflow
# files declare a job *key*. Matching them exactly loses most real repos.

JOBS = [
    {"name": "unit", "display": None},
    {"name": "test", "display": "Run the tests"},
    {"name": "e2e", "display": "E2E ${{ matrix.browser }}"},
]


@pytest.mark.parametrize(
    "reported,expected",
    [
        ("unit", "unit"),  # key, reported verbatim
        ("Run the tests", "test"),  # display name override
        ("unit (3.11)", "unit"),  # matrix suffix on a key
        ("Run the tests (macos-latest)", "test"),  # matrix suffix on a display name
        ("unit (3.11, ubuntu-latest)", "unit"),  # multi-axis matrix
    ],
)
def test_reported_names_resolve_to_their_declared_job(reported, expected):
    assert ci_profile.attribute(reported, JOBS) == expected


def test_unresolvable_expression_name_is_not_guessed():
    assert ci_profile.attribute("E2E chrome", JOBS) is None


def test_unknown_job_resolves_to_nothing():
    assert ci_profile.attribute("Publish release", JOBS) is None


def test_build_profile_attributes_matrix_runs_to_the_declared_job(workflows):
    runs = [
        _job("unit (3.11)", "a", "success", 100, 1),
        _job("unit (3.12)", "a", "success", 140, 2),
    ]
    profile = ci_profile.build_profile(
        workflow_dir=workflows, job_runs=runs, protection={}, threshold_s=300
    )
    assert profile["jobs"]["unit"]["samples"] == 2
    assert "unit" not in profile["unmeasured_jobs"]


def test_runs_that_match_no_declared_job_are_reported_not_dropped(workflows):
    runs = [_job("Publish release", "a", "success", 30, 1)]
    profile = ci_profile.build_profile(
        workflow_dir=workflows, job_runs=runs, protection={}, threshold_s=300
    )
    assert profile["unattributed_runs"] == ["Publish release"]


def test_compiled_bytecode_is_never_offered_as_a_test_to_run(repo):
    cache = repo / "tests" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "test_ledger.cpython-314-pytest-9.0.3.pyc").touch()
    hit, complete = ci_profile.impacted_tests(["src/foreman/ledger.py"], repo)
    assert hit == ["tests/test_ledger.py"]
    assert complete is True


def test_a_non_python_test_suite_still_maps(tmp_path):
    for p in ["src/parser.ts", "tests/test_parser.ts"]:
        f = tmp_path / p
        f.parent.mkdir(parents=True, exist_ok=True)
        f.touch()
    hit, complete = ci_profile.impacted_tests(["src/parser.ts"], tmp_path)
    assert hit == ["tests/test_parser.ts"] and complete is True


# --- issue #26: path filters belong to the event that declared them ----------


def test_push_only_path_filters_are_not_attributed_to_pull_request(tmp_path):
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        textwrap.dedent("""
        name: CI
        on:
          push:
            branches: [main]
            paths: ['src/**', 'tests/**']
          pull_request:
        jobs:
          lint:
            steps: [{run: ruff check}]
    """)
    )
    jobs = {j["name"]: j for j in ci_profile.parse_workflows(d)}
    assert jobs["lint"]["path_filters"] == ["src/**", "tests/**"], "union kept for reference"
    assert jobs["lint"]["pr_path_filters"] == [], "the PR trigger carries no filter"


def test_a_pull_request_path_filter_is_recorded_as_one(tmp_path):
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "docs.yml").write_text(
        textwrap.dedent("""
        name: Docs
        on:
          pull_request:
            paths: ['docs/**']
        jobs:
          docs:
            steps: [{run: make docs}]
    """)
    )
    jobs = {j["name"]: j for j in ci_profile.parse_workflows(d)}
    assert jobs["docs"]["pr_path_filters"] == ["docs/**"]


def test_build_profile_carries_pr_path_filters_through(tmp_path):
    """The unit-level fix is worthless if the assembled profile drops the field."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        textwrap.dedent("""
        name: CI
        on:
          push:
            paths: ['src/**']
          pull_request:
        jobs:
          lint:
            steps: [{run: ruff check}]
    """)
    )
    profile = ci_profile.build_profile(workflow_dir=d, job_runs=[], protection=None)
    assert profile["jobs"]["lint"]["pr_path_filters"] == []
    assert profile["jobs"]["lint"]["path_filters"] == ["src/**"]


# --- issues #32/#33: per-event data, and job names that collide -------------


def test_each_trigger_records_its_own_filters(tmp_path):
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        textwrap.dedent("""
        name: CI
        on:
          push:
            branches: [main]
            paths: ['src/**']
          pull_request:
            paths-ignore: ['**.md']
        jobs:
          lint:
            steps: [{run: ruff check}]
    """)
    )
    events = {j["name"]: j["events"] for j in ci_profile.parse_workflows(d)}["lint"]
    assert events["push"]["branches"] == ["main"]
    assert events["push"]["paths"] == ["src/**"]
    assert events["pull_request"]["paths_ignore"] == ["**.md"]
    assert events["pull_request"]["paths"] == []


def test_a_job_name_reused_in_another_workflow_does_not_overwrite_the_first(tmp_path):
    """Otherwise a release-only `test` job makes the real PR `test` job vanish."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        textwrap.dedent("""
        name: CI
        on: {pull_request: }
        jobs:
          test:
            steps: [{run: pytest}]
    """)
    )
    (d / "release.yml").write_text(
        textwrap.dedent("""
        name: Release
        on: {workflow_dispatch: }
        jobs:
          test:
            steps: [{run: pytest}]
    """)
    )
    profile = ci_profile.build_profile(workflow_dir=d, job_runs=[], protection=None)
    events = profile["jobs"]["test"]["events"]
    assert "pull_request" in events, "the PR trigger must survive the collision"
    assert "workflow_dispatch" in events
    assert set(profile["jobs"]["test"]["triggers"]) == {"pull_request", "workflow_dispatch"}


def test_the_collision_merge_gives_the_same_answer_in_either_file_order(tmp_path):
    """#40's actual claim: requirability must not depend on filename sorting."""
    import land

    plain = "name: A\non: {pull_request: }\njobs:\n  test: {steps: [{run: x}]}\n"
    filtered = (
        "name: B\non:\n  pull_request:\n    paths: ['src/**']\njobs:\n  test: {steps: [{run: y}]}\n"
    )

    answers = []
    for first, second in (("a", "b"), ("b", "a")):
        d = tmp_path / f"{first}{second}" / ".github" / "workflows"
        d.mkdir(parents=True)
        (d / f"{first}.yml").write_text(plain if first == "a" else filtered)
        (d / f"{second}.yml").write_text(filtered if first == "a" else plain)
        profile = ci_profile.build_profile(workflow_dir=d, job_runs=[], protection=None)
        answers.append(land.can_report_on_pr(profile["jobs"]["test"], "main"))

    assert answers[0] == answers[1], "file order changed the gate's behaviour"
    assert answers[0] is True, "one unconditional declaration means a check will appear"


# --- issue #48: GitHub's filter-pattern syntax --------------------------------
#
# `globs.compile_glob` compiles the branch, tag and path filters read out of the
# workflows above, so the cheat-sheet cases live here beside the profile that
# feeds them to the gate.


def test_a_character_class_matches_one_alphanumeric_listed_in_the_brackets():
    assert globs.compile_glob("[CB]at").match("Cat")
    assert globs.compile_glob("[CB]at").match("Bat")
    assert not globs.compile_glob("[CB]at").match("Rat")


def test_a_range_in_a_character_class_matches_every_character_in_the_range():
    """GitHub's own example: v[12].[0-9]+.[0-9]+ is major version 1 or 2."""
    pattern = globs.compile_glob("v[12].[0-9]+.[0-9]+")
    assert pattern.match("v1.10.1")
    assert pattern.match("v2.0.0")
    assert not pattern.match("v3.0.0")
    assert not pattern.match("v1.10.x")


def test_a_question_mark_makes_the_preceding_character_optional():
    """GitHub's `?` is a quantifier, not fnmatch's match-exactly-one-character."""
    pattern = globs.compile_glob("*.jsx?")
    assert pattern.match("page.js")
    assert pattern.match("page.jsx")
    assert not pattern.match("page.jsxx")


def test_a_plus_repeats_the_preceding_character_one_or_more_times():
    pattern = globs.compile_glob("release/v1+")
    assert pattern.match("release/v1")
    assert pattern.match("release/v111")
    assert not pattern.match("release/v")


def test_a_character_class_never_crosses_a_directory_separator():
    assert not globs.compile_glob("src/[a-z]/x.py").match("src/a/b/x.py")


def test_a_bracket_expression_github_does_not_support_stays_a_literal():
    """Ranges cover a-z, A-Z and 0-9 only. Inventing a meaning for anything else
    — a negated class, say — would silently change which paths are protected."""
    assert globs.compile_glob("release-[!x].txt").match("release-[!x].txt")
    assert not globs.compile_glob("release-[!x].txt").match("release-y.txt")
    assert globs.compile_glob("v[9-0]").match("v[9-0]")  # a reversed range is not a range
    assert globs.compile_glob("logs/[unclosed").match("logs/[unclosed")


def test_a_negated_class_branch_filter_keeps_the_job_out_of_the_gate(tmp_path):
    """#48's repro: GitHub excludes base release/v1, so no check ever appears.

    Reading the negative pattern as a literal leaves the job requirable, and the
    gate then waits for a check that cannot arrive until the staleness timer
    escalates it.
    """
    import land

    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        textwrap.dedent("""
        name: CI
        on:
          pull_request:
            branches: ['**', '!release/v[12]']
        jobs:
          test:
            steps: [{run: pytest}]
    """)
    )
    spec = ci_profile.build_profile(workflow_dir=d, job_runs=[], protection=None)["jobs"]["test"]
    assert land.can_report_on_pr(spec, "release/v1") is False
    assert land.can_report_on_pr(spec, "release/v3") is True


def test_a_positive_class_branch_filter_makes_the_job_requirable(tmp_path):
    """The other direction from #48: the literal reading under-requires."""
    import land

    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        textwrap.dedent("""
        name: CI
        on:
          pull_request:
            branches: ['v[12].x']
        jobs:
          test:
            steps: [{run: pytest}]
    """)
    )
    spec = ci_profile.build_profile(workflow_dir=d, job_runs=[], protection=None)["jobs"]["test"]
    assert land.can_report_on_pr(spec, "v1.x") is True
    assert land.can_report_on_pr(spec, "v3.x") is False


# --- issue #50: the hyphenated ignore keys ------------------------------------
#
# `branches-ignore` and `tags-ignore` are the only filters whose YAML spelling
# differs from the profile field they land in, so they are the only ones a
# copy-paste regression to the underscore spelling would silently empty. An
# empty ignore list reads as "no filter", which makes an unrunnable job
# requirable and hangs the gate — so assert the spelling through the parser
# rather than hand-building the dict the parser is supposed to produce.


def test_parse_workflows_reads_the_hyphenated_ignore_keys(tmp_path):
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        textwrap.dedent("""
        name: CI
        on:
          pull_request:
            branches-ignore: ['release/**']
          push:
            tags-ignore: ['v*']
        jobs:
          test:
            steps: [{run: pytest}]
    """)
    )
    events = {j["name"]: j["events"] for j in ci_profile.parse_workflows(d)}["test"]
    assert events["pull_request"]["branches_ignore"] == ["release/**"]
    assert events["push"]["tags_ignore"] == ["v*"]


def test_a_branches_ignore_on_the_base_keeps_the_job_out_of_the_gate(tmp_path):
    """The consequence of losing that key: a job GitHub never runs on this PR
    would be waited for until the staleness timer escalated the batch."""
    import land

    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        textwrap.dedent("""
        name: CI
        on:
          pull_request:
            branches-ignore: [main]
        jobs:
          test:
            steps: [{run: pytest}]
    """)
    )
    spec = ci_profile.build_profile(workflow_dir=d, job_runs=[], protection=None)["jobs"]["test"]
    assert land.can_report_on_pr(spec, "main") is False
    assert land.can_report_on_pr(spec, "develop") is True


def test_a_tags_ignore_push_trigger_keeps_the_job_out_of_the_gate(tmp_path):
    """A push filtered by tags only never fires on a branch push, so it can
    never report on a pull request."""
    import land

    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        textwrap.dedent("""
        name: CI
        on:
          push:
            tags-ignore: ['v*']
        jobs:
          test:
            steps: [{run: pytest}]
    """)
    )
    spec = ci_profile.build_profile(workflow_dir=d, job_runs=[], protection=None)["jobs"]["test"]
    assert land.can_report_on_pr(spec, "main") is False


# --- issue #51: merging two declarations of one job name ----------------------


def _two_workflows(tmp_path, first_on: str, second_on: str) -> list[bool]:
    """Requirability of job `test` on a PR into main, with the two workflow
    files written in both filename orders."""
    import land

    answers = []
    for names in (("a", "b"), ("b", "a")):
        d = tmp_path / "".join(names) / ".github" / "workflows"
        d.mkdir(parents=True)
        for name, on in zip(names, (first_on, second_on), strict=True):
            (d / f"{name}.yml").write_text(
                f"name: {name}\non:\n{textwrap.indent(textwrap.dedent(on).strip(), '  ')}\n"
                "jobs:\n  test: {steps: [{run: pytest}]}\n"
            )
        profile = ci_profile.build_profile(workflow_dir=d, job_runs=[], protection=None)
        answers.append(land.can_report_on_pr(profile["jobs"]["test"], "main"))
    return answers


def test_the_collision_merge_keeps_the_declaration_that_can_still_report(tmp_path):
    """#51's repro. Both declarations carry exactly one filter, so a filter-count
    proxy cannot separate them and the answer falls to filename order. Only the
    branch-filtered one produces a check while the PR is open, and it does so on
    every PR into main, so the job is requirable however the files are named.
    """
    answers = _two_workflows(
        tmp_path,
        "pull_request:\n  types: [closed]",
        "pull_request:\n  branches: [main]",
    )
    assert answers == [True, True]


def test_two_branch_filtered_declarations_merge_into_the_union_of_their_branches(tmp_path):
    """Neither declaration covers the other, but their disjunction is exactly
    one branch list, so nothing has to be thrown away."""
    import land

    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    for name, branch in (("a", "main"), ("b", "develop")):
        (d / f"{name}.yml").write_text(
            f"name: {name}\non:\n  pull_request:\n    branches: [{branch}]\n"
            "jobs:\n  test: {steps: [{run: pytest}]}\n"
        )
    spec = ci_profile.build_profile(workflow_dir=d, job_runs=[], protection=None)["jobs"]["test"]
    assert land.can_report_on_pr(spec, "main") is True
    assert land.can_report_on_pr(spec, "develop") is True
    assert land.can_report_on_pr(spec, "release") is False


def test_the_collision_merge_never_widens_two_declarations_into_no_filter(tmp_path):
    """`branches: [main]` OR `paths: [src/**]` is not "unconditional": a PR into
    develop touching only docs runs neither. Merging key by key would say
    otherwise and hang the gate on a check that never appears.
    """
    import land

    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "a.yml").write_text(
        "name: a\non:\n  pull_request:\n    branches: [main]\n"
        "jobs:\n  test: {steps: [{run: pytest}]}\n"
    )
    (d / "b.yml").write_text(
        "name: b\non:\n  pull_request:\n    paths: ['src/**']\n"
        "jobs:\n  test: {steps: [{run: pytest}]}\n"
    )
    spec = ci_profile.build_profile(workflow_dir=d, job_runs=[], protection=None)["jobs"]["test"]
    assert land.can_report_on_pr(spec, "main") is True
    assert land.can_report_on_pr(spec, "develop") is False


def test_a_type_that_always_reports_can_also_report_while_the_pull_request_is_open():
    """The two sets answer different questions and must not be conflated.

    `ci_profile.OPEN_PR_TYPES` is the broad one: can this declaration ever put a
    check on a live PR, as opposed to `types: [closed]`, which cannot.
    `land.PR_UNCONDITIONAL_TYPES` is the narrow one: will it report on *every*
    PR, which is what makes a job safe to require. They were briefly equal, and
    that was issue #49 — requiring a job gated on `ready_for_review` hangs the
    gate for every PR that opens ready. The surviving invariant is containment:
    a type that always reports must at least be able to report while open.
    """
    import land

    assert set(land.PR_UNCONDITIONAL_TYPES) <= set(ci_profile.OPEN_PR_TYPES)


# --- issue #74: the profile is written where every reader looks ---------------


@pytest.fixture
def worktree(tmp_path):
    """The layout `commands/build.md` prescribes: `.foreman` lives one repo up."""

    def git(*args):
        subprocess.run(
            ["git", "-c", "user.email=f@example.com", "-c", "user.name=foreman", *args],
            cwd=str(checkout),
            check=True,
            capture_output=True,
        )

    checkout = tmp_path / "repo"
    checkout.mkdir()
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "root")
    linked = tmp_path / "foreman-b-001"
    git("worktree", "add", "-q", str(linked), "-b", "foreman/b-001")
    ledger.init(checkout)
    return checkout, linked


def test_probe_writes_the_profile_into_the_repository_when_run_from_a_worktree(
    worktree, monkeypatch, capsys
):
    """Written against the caller, the default created a second `.foreman` in the
    worktree, and the profile every other script anchors to stayed missing."""
    checkout, linked = worktree
    monkeypatch.setattr(
        ci_profile,
        "probe",
        lambda *a, **k: {
            "jobs": {},
            "cheap_tier_s": None,
            "expensive_tier_s": None,
            "unmeasured_jobs": [],
        },
    )
    monkeypatch.chdir(linked)

    assert ci_profile.main(["probe", "--repo", "me/mine"]) == 0
    written = Path(json.loads(capsys.readouterr().out)["written"])
    assert written == checkout / ledger.LEDGER_DIR / ledger.PROFILE_FILE
    assert written.is_file()
    assert not (linked / ledger.LEDGER_DIR).exists()


# --- a scalar filter is one pattern, not a list of its characters ------------


def test_a_scalar_branch_or_path_filter_is_read_as_one_pattern(tmp_path):
    """`paths: docs/**` came back as ['*', '/', 'c', 'd', 'o', 's'], and the
    bare `*` then matched every top-level file in the repository; `branches:
    main` was four one-letter branch names. `on:` and `needs:` already had
    this guard."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        textwrap.dedent("""
        name: CI
        on:
          pull_request:
            paths: docs/**
            types: opened
          push:
            branches: main
            tags: v*
            paths-ignore: '**.md'
        jobs:
          docs: {steps: [{run: make docs}]}
    """)
    )
    (job,) = ci_profile.parse_workflows(d)
    assert job["path_filters"] == ["docs/**"]
    assert job["pr_path_filters"] == ["docs/**"]
    assert job["events"]["pull_request"]["paths"] == ["docs/**"]
    assert job["events"]["pull_request"]["types"] == ["opened"]
    assert job["events"]["push"]["branches"] == ["main"]
    assert job["events"]["push"]["tags"] == ["v*"]
    assert job["events"]["push"]["paths_ignore"] == ["**.md"]


def test_a_scalar_branch_filter_gates_the_job_the_way_a_list_would(tmp_path):
    import land

    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        "name: CI\non:\n  pull_request:\n    branches: main\n"
        "jobs:\n  test: {steps: [{run: pytest}]}\n"
    )
    spec = ci_profile.build_profile(workflow_dir=d, job_runs=[], protection=None)["jobs"]["test"]
    assert land.can_report_on_pr(spec, "main") is True
    assert land.can_report_on_pr(spec, "develop") is False


def test_a_mapping_where_a_filter_belongs_reads_as_no_filter(tmp_path):
    """Not a shape Actions accepts. No filter is the conservative reading: a
    job with none fires on everything."""
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        "name: CI\non:\n  pull_request:\n    paths: {oops: true}\n"
        "jobs:\n  test: {steps: [{run: pytest}]}\n"
    )
    (job,) = ci_profile.parse_workflows(d)
    assert job["path_filters"] == []
    assert job["events"]["pull_request"]["paths"] == []


def test_the_collision_merge_unions_needs_whichever_file_sorts_first(tmp_path):
    """`triggers` and the filters were merged; `needs` was whichever
    declaration was read first, so renaming a workflow file changed the job
    graph the profile reported."""
    answers = []
    for names in (("a", "b"), ("b", "a")):
        d = tmp_path / "".join(names) / ".github" / "workflows"
        d.mkdir(parents=True)
        (d / f"{names[0]}.yml").write_text(
            "name: x\non: {pull_request: }\njobs:\n  lint: {steps: [{run: ruff}]}\n"
            "  test: {needs: [lint], steps: [{run: pytest}]}\n"
        )
        (d / f"{names[1]}.yml").write_text(
            "name: y\non: {workflow_dispatch: }\njobs:\n  test: {steps: [{run: pytest}]}\n"
        )
        profile = ci_profile.build_profile(workflow_dir=d, job_runs=[], protection=None)
        answers.append(profile["jobs"]["test"]["needs"])
    assert answers == [["lint"], ["lint"]]
