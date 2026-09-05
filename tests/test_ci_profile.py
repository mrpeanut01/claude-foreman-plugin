"""CI profile: learn the repo's job graph, costs, flake rates, and test mapping."""

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ci_profile  # noqa: E402

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
