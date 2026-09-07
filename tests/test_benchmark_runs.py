"""Benchmarks and simulations: measurements, not verdicts.

A test says the code is correct or it is not, and that is why a merge waits for
it. A benchmark says the code took 4.2 seconds. Nothing about that number says
the diff is wrong, so waiting for it holds a correctness fix behind an answer to
a different question — and running it on a diff that cannot move it pays the
full wall clock to reproduce noise.

These tests pin the three consequences: a benchmark is recognised, it is only
launched when something says this diff can move it, and it never becomes the
reason a merge is waiting.
"""

import json
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ci_profile  # noqa: E402
import gate  # noqa: E402
import globs  # noqa: E402
import land  # noqa: E402


def _job(name, display=None, workflow="CI", workflow_file="ci.yml"):
    return {
        "name": name,
        "display": display,
        "workflow": workflow,
        "workflow_file": workflow_file,
    }


# --- recognising a benchmark --------------------------------------------------


def test_a_configured_job_is_a_benchmark_on_the_operator_s_word():
    kinds = ci_profile.classify_kinds([_job("nightly")], declared=["nightly"])
    assert kinds["nightly"] == (ci_profile.KIND_BENCHMARK, "config")


def test_a_job_that_calls_itself_a_benchmark_is_recognised_but_only_as_a_guess():
    """`kind_source` is the whole safety mechanism: `land.py` acts on `config`
    and refuses to act on `name`, so the two must never collapse into one."""
    kinds = ci_profile.classify_kinds([_job("bench"), _job("load-test"), _job("soak")])
    assert {k: v for k, v in kinds.items()} == {
        "bench": (ci_profile.KIND_BENCHMARK, "name"),
        "load-test": (ci_profile.KIND_BENCHMARK, "name"),
        "soak": (ci_profile.KIND_BENCHMARK, "name"),
    }


def test_a_configured_match_outranks_the_name_guess():
    kinds = ci_profile.classify_kinds([_job("bench")], declared=["bench"])
    assert kinds["bench"] == (ci_profile.KIND_BENCHMARK, "config")


@pytest.mark.parametrize(
    "name",
    [
        "perform-migration-check",  # `perf` as a substring of `perform`
        "similarity-check",  # `sim` as a substring of `similarity`
        "simple-lint",
        "benchmarking-docs-build",  # hyphen is a boundary, but this one IS a build
    ],
)
def test_the_vocabulary_is_word_anchored(name):
    """A substring match here reclassifies a correctness job, and a
    reclassified correctness job is one the local gate stops running. The
    fourth case is the honest limit: `benchmarking-docs-build` does match, and
    the way to say otherwise is `benchmark_jobs`, not a longer regex."""
    kind, source = ci_profile.classify_kinds([_job(name)])[name]
    if name == "benchmarking-docs-build":
        assert (kind, source) == (ci_profile.KIND_BENCHMARK, "name")
    else:
        assert (kind, source) == (ci_profile.KIND_TEST, "default")


def test_the_display_name_and_the_workflow_name_are_checked_too():
    """A repo that hides its soak behind `name: Nightly` is caught by the
    workflow; one whose workflow is plain `CI` is caught by the job key."""
    jobs = [
        _job("job1", display="Throughput regression"),
        _job("job2", workflow="Performance"),
    ]
    kinds = ci_profile.classify_kinds(jobs)
    assert kinds["job1"][0] == ci_profile.KIND_BENCHMARK
    assert kinds["job2"][0] == ci_profile.KIND_BENCHMARK


def test_an_ordinary_job_is_a_test():
    assert ci_profile.classify_kinds([_job("unit")])["unit"] == (ci_profile.KIND_TEST, "default")


# --- what the profile records -------------------------------------------------


@pytest.fixture
def workflow_dir(tmp_path):
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "ci.yml").write_text(
        textwrap.dedent("""
        name: CI
        on: [pull_request]
        jobs:
          unit:
            steps: [{run: pytest}]
          bench:
            steps: [{run: make bench}]
          e2e-simulation:
            steps: [{run: make sim}]
    """)
    )
    (d / "perf.yml").write_text(
        textwrap.dedent("""
        name: Perf
        on:
          pull_request:
            paths: ['src/**', '!src/**/*.md']
        jobs:
          throughput:
            steps: [{run: make throughput}]
    """)
    )
    return d


@pytest.fixture
def profile(workflow_dir):
    return ci_profile.build_profile(
        workflow_dir,
        job_runs=[],
        protection=None,
        benchmark_jobs=["e2e-simulation"],
    )


def test_the_profile_records_kind_beside_tier(profile):
    """Two axes, not one. `tier` is what a job costs; `kind` is what its result
    means. A 40-second smoke simulation is cheap and still not a verdict."""
    assert profile["jobs"]["unit"]["kind"] == ci_profile.KIND_TEST
    assert profile["jobs"]["e2e-simulation"]["kind"] == ci_profile.KIND_BENCHMARK
    assert profile["jobs"]["e2e-simulation"]["kind_source"] == "config"
    assert profile["jobs"]["bench"]["kind_source"] == "name"
    assert profile["benchmark_jobs"] == ["bench", "e2e-simulation", "throughput"]


# --- does this diff warrant a run? --------------------------------------------


def _plan(profile, changed, config=None):
    return ci_profile.benchmark_plan(profile, changed, config or {})


def test_a_workflow_path_filter_is_the_answer_when_there_is_one(profile):
    """GitHub already enforces this filter on every push. Deciding differently
    here would put the loop at odds with what CI actually does."""
    decisions = {d["job"]: d for d in _plan(profile, ["src/engine.py"])["decisions"]}
    assert decisions["throughput"]["basis"] == "workflow path filter"
    assert "throughput" in _plan(profile, ["src/engine.py"])["run"]
    assert "throughput" in _plan(profile, ["tools/build.sh"])["skip"]


def test_a_negated_path_filter_excludes_by_github_s_last_match_wins_rule(profile):
    """`['src/**', '!src/**/*.md']` means source but not its prose. Read as a
    first-match scan it means the opposite for exactly the files it excludes."""
    assert "throughput" in _plan(profile, ["src/README.md"])["skip"]
    assert "throughput" in _plan(profile, ["src/engine.py"])["run"]


def test_paths_ignore_selects_on_the_files_it_does_not_cover(tmp_path):
    d = tmp_path / ".github" / "workflows"
    d.mkdir(parents=True)
    (d / "b.yml").write_text(
        textwrap.dedent("""
        name: CI
        on:
          pull_request:
            paths-ignore: ['docs/**']
        jobs:
          bench:
            steps: [{run: make bench}]
    """)
    )
    profile = ci_profile.build_profile(d, job_runs=[], protection=None)
    assert _plan(profile, ["docs/a.md"])["skip"] == ["bench"]
    assert _plan(profile, ["docs/a.md", "src/a.py"])["run"] == ["bench"]


def test_configured_benchmark_paths_decide_a_job_whose_workflow_declares_none(profile):
    config = {"benchmark_paths": {"bench": ["src/engine/**"]}}
    assert "bench" in _plan(profile, ["src/engine/loop.py"], config)["run"]
    assert "bench" in _plan(profile, ["src/ui/button.tsx"], config)["skip"]


def test_a_flat_benchmark_paths_list_covers_every_benchmark(profile):
    config = {"benchmark_paths": ["src/**"]}
    plan = _plan(profile, ["src/engine/loop.py"], config)
    assert set(plan["run"]) == {"bench", "e2e-simulation", "throughput"}


def test_a_documentation_only_diff_warrants_nothing_even_undeclared(profile):
    """The one thing that can be said with no declaration at all: prose has no
    runtime, so no reading can move because of it."""
    plan = _plan(profile, ["README.md", "docs/guide.md"])
    assert plan["run"] == []
    assert set(plan["skip"]) == {"bench", "e2e-simulation", "throughput"}


def test_an_undeclared_benchmark_on_a_code_diff_is_unknown_and_launches_nothing(profile):
    """Neither answer is honest, so neither is given. Guessing `true` restores
    the every-PR spend this exists to stop; guessing `false` silently retires a
    benchmark. It becomes a gap to declare instead."""
    plan = _plan(profile, ["src/engine/loop.py"])
    assert plan["unknown"] == ["bench", "e2e-simulation"]
    assert plan["run"] == ["throughput"]  # the one job that declared its own paths


def test_unknown_never_reaches_run(profile):
    """`run` is the only field that spends anything: a job is launched when
    something says it should be, never because nothing said it should not."""
    for changed in (["src/a.py"], ["Dockerfile"], ["src/engine/loop.py", "README.md"]):
        plan = _plan(profile, changed)
        assert not set(plan["run"]) & set(plan["unknown"])


def test_a_test_job_is_never_in_the_plan_at_all(profile):
    """The plan only ever excuses measurements. A correctness job appearing here
    would be one the loop had stopped waiting for."""
    assert "unit" not in [d["job"] for d in _plan(profile, ["src/a.py"])["decisions"]]


# --- the gate never waits on a measurement ------------------------------------


SHA = "abc1234def5678"


def _checks(**states):
    return [{"name": n, "state": s, "head_sha": SHA} for n, s in states.items()]


def test_a_declared_benchmark_does_not_hold_the_merge(profile):
    checks = _checks(unit="SUCCESS", bench="SUCCESS", **{"e2e-simulation": "PENDING"})
    assert land.ci_gate(checks, profile, base_branch="main", expected_sha=SHA) == "full_green"
    summary = land.classify_checks(checks, profile, SHA)
    assert summary["advisory_pending"] == ["e2e-simulation"]
    assert summary["actionable_pending"] == []


def test_a_declared_benchmark_that_fails_is_advisory_not_a_red_gate(profile):
    """A slower number is not a wrong answer. It is worth knowing and worth
    filing; it is not grounds to refuse a correctness fix."""
    checks = _checks(unit="SUCCESS", bench="SUCCESS", **{"e2e-simulation": "FAILURE"})
    summary = land.classify_checks(checks, profile, SHA)
    assert summary["advisory_failed"] == ["e2e-simulation"]
    assert summary["failed"] == []


def test_a_name_guessed_benchmark_still_holds_the_merge(profile):
    """The load-bearing asymmetry. A guess may narrow what is *spent*; it may
    never be the reason a merge stopped waiting for a check, because a job named
    `perf-regression-test` that really does gate the merge would be waved
    through on nothing but its name."""
    checks = _checks(unit="SUCCESS", bench="PENDING")
    assert land.ci_gate(checks, profile, base_branch="main", expected_sha=SHA) == "pending"
    assert land.classify_checks(checks, profile, SHA)["actionable_pending"] == ["bench"]


def test_branch_protection_outranks_the_config(profile):
    """A repo that made its benchmark a required check has said the merge waits
    for it. No config key gets to contradict branch protection."""
    protected = {
        **profile,
        "protection_known": True,
        "required_checks": ["unit", "e2e-simulation"],
    }
    checks = _checks(unit="SUCCESS", bench="SUCCESS", **{"e2e-simulation": "PENDING"})
    assert land.ci_gate(checks, protected, base_branch="main", expected_sha=SHA) == "pending"
    assert land.classify_checks(checks, protected, SHA)["actionable_pending"] == ["e2e-simulation"]


def test_a_declared_benchmark_is_not_requirable_under_unknown_protection(profile):
    """Otherwise the gate hangs: excused from reporting by `_is_advisory`, but
    still counted in the set of jobs whose report the gate waits for."""
    assert land.is_declared_benchmark("e2e-simulation", profile) is True
    assert land.is_declared_benchmark("bench", profile) is False
    checks = _checks(unit="SUCCESS", bench="SUCCESS")
    assert land.ci_gate(checks, profile, base_branch="main", expected_sha=SHA) == "full_green"


def test_a_real_test_still_holds_the_merge(profile):
    checks = _checks(unit="PENDING", bench="SUCCESS", **{"e2e-simulation": "SUCCESS"})
    assert land.ci_gate(checks, profile, base_branch="main", expected_sha=SHA) == "pending"


# --- the local gate never runs one --------------------------------------------


def test_the_local_gate_leaves_benchmarks_to_ci(profile, workflow_dir):
    chosen, deferred, _ = gate.select_jobs(profile, workflow_dir)
    assert [j["name"] for j in chosen] == ["unit"]
    assert {d["job"] for d in deferred} == {"bench", "e2e-simulation", "throughput"}
    assert all("benchmark" in d["reason"] for d in deferred)


def test_the_local_gate_leaves_benchmarks_to_ci_even_unprofiled(workflow_dir):
    """Unprofiled is exactly when a nightly soak is most likely to be swept into
    the local gate, since every job then reads as tier `unknown`, which is
    runnable."""
    chosen, deferred, _ = gate.select_jobs(None, workflow_dir)
    assert [j["name"] for j in chosen] == ["unit"]
    assert {d["job"] for d in deferred} == {"bench", "e2e-simulation", "throughput"}


# --- the parallel effort ------------------------------------------------------


def test_a_warranted_benchmark_becomes_work_of_its_own(profile):
    """It must not go into the batch's PR. The batch is a correctness fix; the
    benchmark is a number, and putting the number in front of the fix is the
    serialisation this whole decision exists to avoid."""
    plan = _plan(profile, ["src/engine.py"])
    findings = ci_profile.benchmark_findings(plan, batch="b-007")
    run = [f for f in findings if f["summary"].startswith("Run the throughput")]
    assert len(run) == 1
    assert "b-007" in run[0]["summary"]
    assert run[0]["file"] == ".github/workflows/perf.yml"


def test_an_undeclared_benchmark_becomes_a_request_to_declare_it(profile):
    """The gap is worth an issue precisely because nothing else in the loop will
    ever produce one, and one line of config fixes every later decision."""
    findings = ci_profile.benchmark_findings(_plan(profile, ["src/engine.py"]))
    gaps = [f for f in findings if f["summary"].startswith("Declare in benchmark_paths")]
    assert [f["file"] for f in gaps] == [".foreman/config.json"]
    assert gaps[0]["summary"].endswith("bench, e2e-simulation")


def test_every_undeclared_benchmark_shares_one_issue(profile):
    """One per job produces titles a word apart, which the filer reads as
    duplicates — correctly, but the survivor names one job and the others'
    gaps are then closed by an issue that never mentions them. One edit to one
    file is one issue, so the shape is the fix rather than the threshold."""
    import findings as findings_mod

    findings = ci_profile.benchmark_findings(_plan(profile, ["src/engine.py"]))
    result = findings_mod.plan(findings, {"batch": "b-1", "round": 1}, ["enhancement"], [])
    assert result["skipped"] == []
    assert len(result["file"]) == len(findings)


def test_a_skipped_benchmark_produces_no_work_at_all(profile):
    """Nothing happened and nothing needs to. A skip that filed an issue would
    turn the saving into a queue of noise."""
    assert ci_profile.benchmark_findings(_plan(profile, ["README.md"])) == []


def test_the_findings_are_the_shape_findings_py_already_files(profile, monkeypatch):
    """No second filer: `benchmark-plan` emits what `findings.plan` consumes, so
    dedupe, labelling and the ledger record all come for free."""
    import findings as findings_mod

    plan = _plan(profile, ["src/engine.py"])
    context = {"batch": "b-007", "round": 1, "repo": "me/mine", "source": "the benchmark plan"}
    result = findings_mod.plan(
        ci_profile.benchmark_findings(plan, "b-007"), context, ["enhancement"], []
    )
    assert result["unusable"] == []
    assert len(result["file"]) == 2
    assert all(i["labels"] == ["enhancement"] for i in result["file"])


def test_a_benchmark_issue_does_not_claim_the_reviewer_raised_it(profile):
    """The provenance line is the only trace back. Sending a reader to a verdict
    file that never mentions this finding wastes the one link it carries."""
    import findings as findings_mod

    issue = findings_mod.to_issue(
        {"summary": "Run the bench benchmark", "severity": "low"},
        {"batch": "b-007", "round": 1, "source": "the benchmark plan"},
        ["enhancement"],
    )
    assert "Raised by the benchmark plan" in issue["body"]
    assert "independent review" not in issue["body"]


def test_the_review_keeps_its_wording_when_no_source_is_given():
    import findings as findings_mod

    issue = findings_mod.to_issue(
        {"summary": "Off by one", "severity": "high"},
        {"batch": "b-001", "pr": 7, "round": 2},
        ["bug"],
    )
    assert "Raised by the independent review of PR #7" in issue["body"]


# --- the path rule the whole decision rests on --------------------------------


@pytest.mark.parametrize(
    "path,patterns,expected",
    [
        ("src/a.py", ["src/**"], True),
        ("src/a.md", ["src/**", "!src/**/*.md"], False),
        ("src/a.py", ["src/**", "!src/**/*.md"], True),
        # Order decides: the same two patterns reversed mean the opposite.
        ("src/a.md", ["!src/**/*.md", "src/**"], True),
        ("docs/a.md", ["src/**"], False),
        # Exclusions only: everything they do not name is included.
        ("docs/a.md", ["!src/**"], True),
        ("src/a.py", ["!src/**"], False),
        # A filter that declares nothing has selected nothing.
        ("anything", [], False),
    ],
)
def test_path_included_follows_github_last_match_wins(path, patterns, expected):
    assert globs.path_included(path, patterns) is expected


# --- the CLI ------------------------------------------------------------------


def test_the_cli_emits_a_plan_and_its_findings(profile, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ci_profile.ledger, "load_profile", lambda *a, **k: profile)
    monkeypatch.setattr(ci_profile, "_quiet_config", lambda *a, **k: {})
    assert ci_profile.main(["benchmark-plan", "--changed", "src/engine.py", "--batch", "b-1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run"] == ["throughput"]
    assert out["unknown"] == ["bench", "e2e-simulation"]
    assert len(out["findings"]) == 2  # one run, one gap covering both


def test_the_cli_says_so_when_there_is_no_profile_to_decide_from(monkeypatch, capsys):
    """An empty plan and "this repo has no benchmarks" are the same JSON, and
    the difference is whether a benchmark is being silently excused."""
    monkeypatch.setattr(ci_profile.ledger, "load_profile", lambda *a, **k: {})
    monkeypatch.setattr(ci_profile, "_quiet_config", lambda *a, **k: {})
    assert ci_profile.main(["benchmark-plan", "--changed", "src/a.py"]) == 0
    assert "no CI profile" in capsys.readouterr().err


def test_one_required_matrix_cell_makes_the_whole_declared_benchmark_gate(profile):
    """`required` rounds partial coverage up by design: protection naming
    `e2e-simulation (fast)` means the job can block a merge, and the config key
    must not quietly excuse the cells protection did not name."""
    protected = json.loads(json.dumps(profile))
    protected["protection_known"] = True
    protected["required_checks"] = ["e2e-simulation (fast)"]
    protected["jobs"]["e2e-simulation"]["required"] = True
    assert land.is_declared_benchmark("e2e-simulation (slow)", protected) is False
    checks = _checks(unit="SUCCESS", bench="SUCCESS", **{"e2e-simulation (slow)": "PENDING"})
    assert land.ci_gate(checks, protected, base_branch="main", expected_sha=SHA) == "pending"


# --- a profile built before any of this existed -------------------------------


def test_a_profile_with_no_kind_field_behaves_exactly_as_before(profile, workflow_dir):
    """Every reader uses `.get`, so an unrefreshed profile degrades to the old
    behaviour rather than crashing or excusing a job it knows nothing about."""
    old = json.loads(json.dumps(profile))
    for spec in old["jobs"].values():
        spec.pop("kind", None)
        spec.pop("kind_source", None)
    old.pop("benchmark_jobs", None)

    assert land.is_declared_benchmark("e2e-simulation", old) is False
    checks = _checks(unit="SUCCESS", bench="SUCCESS", **{"e2e-simulation": "PENDING"})
    assert land.ci_gate(checks, old, base_branch="main", expected_sha=SHA) == "pending"
    assert ci_profile.benchmark_plan(old, ["src/a.py"], {}) == {
        "decisions": [],
        "run": [],
        "skip": [],
        "unknown": [],
        "seconds_not_spent": 0,
        "recommendation": "no benchmark on this repo can be moved by this diff",
    }
    chosen, _, _ = gate.select_jobs(old, workflow_dir)
    assert "bench" in [j["name"] for j in chosen]


def test_an_empty_diff_warrants_nothing(profile):
    """A push with no files changed cannot have moved a number. GitHub's own
    path filters are any-tests over the diff, so they answer the same way."""
    plan = _plan(profile, [])
    assert plan["run"] == []
