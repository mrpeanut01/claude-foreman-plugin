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
    (d / "ci.yml").write_text(textwrap.dedent("""
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
    """))
    (d / "nightly.yml").write_text(textwrap.dedent("""
        name: Nightly
        on:
          schedule: [{cron: '0 3 * * *'}]
        jobs:
          soak:
            steps: [{run: make soak}]
    """))
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
    jobs = [_job("unit", f"sha{i}", "success", s, run_id=i)
            for i, s in enumerate([60, 90, 120, 150, 600])]
    stats = ci_profile.duration_stats(jobs)["unit"]
    assert stats["n"] == 5
    assert stats["p50"] == 120
    assert stats["p95"] == 600


def test_duration_stats_ignore_cancelled_and_skipped_jobs():
    jobs = [_job("unit", "a", "success", 100, 1),
            _job("unit", "b", "cancelled", 5, 2),
            _job("unit", "c", "skipped", 1, 3)]
    assert ci_profile.duration_stats(jobs)["unit"]["n"] == 1


# --- tiers --------------------------------------------------------------------

def test_tiers_split_on_the_configured_threshold():
    stats = {"lint": {"p95": 45}, "unit": {"p95": 240}, "integration": {"p95": 2400}}
    tiers = ci_profile.classify_tiers(stats, threshold_s=300)
    assert tiers == {"lint": "cheap", "unit": "cheap", "integration": "expensive"}


# --- flake rate ---------------------------------------------------------------

def test_flake_is_a_job_that_both_failed_and_passed_on_one_sha():
    jobs = [_job("integration", "sha1", "failure", 100, 1),
            _job("integration", "sha1", "success", 100, 2),
            _job("integration", "sha2", "success", 100, 3),
            _job("integration", "sha3", "success", 100, 4),
            _job("integration", "sha4", "success", 100, 5)]
    assert ci_profile.flake_rates(jobs)["integration"] == pytest.approx(0.25)


def test_a_sha_that_only_ever_failed_is_a_real_failure_not_a_flake():
    jobs = [_job("unit", "sha1", "failure", 10, 1), _job("unit", "sha1", "failure", 10, 2)]
    assert ci_profile.flake_rates(jobs)["unit"] == 0.0


# --- required checks ----------------------------------------------------------

def test_required_checks_read_from_branch_protection():
    protection = {"required_status_checks": {"contexts": ["lint", "unit"],
                                             "checks": [{"context": "integration"}]}}
    assert ci_profile.required_checks(protection) == ["integration", "lint", "unit"]


def test_missing_protection_yields_no_required_checks():
    assert ci_profile.required_checks({}) == []
    assert ci_profile.required_checks(None) == []


# --- test impact --------------------------------------------------------------

@pytest.fixture
def repo(tmp_path):
    for p in ["src/foreman/ledger.py", "src/foreman/api.py",
              "tests/test_ledger.py", "tests/integration/test_api.py"]:
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
        workflow_dir=workflows, job_runs=jobs_json,
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
    profile = ci_profile.build_profile(workflow_dir=workflows, job_runs=[],
                                       protection={}, threshold_s=300)
    assert profile["jobs"]["soak"]["tier"] == "unmeasured"
    assert profile["unmeasured_jobs"] == ["integration", "lint", "soak", "unit"]
