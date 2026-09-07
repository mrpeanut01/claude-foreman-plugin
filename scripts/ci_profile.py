#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml"]
# ///
"""Learn what this repo's CI actually costs, so the loop can stop overspending it.

Produces .foreman/ci-profile.json: the job graph, observed durations, which
jobs are required to merge, which ones flake, and how a diff maps to tests.
Every number here is measured from real runs, not declared in a config file.

CLI:
    ci_profile.py probe --repo OWNER/NAME [--runs 50] [--out PATH]
    ci_profile.py impact --changed FILE [FILE ...]
    ci_profile.py benchmark-plan --changed FILE [FILE ...] [--batch b-001]
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger  # noqa: E402
from globs import matches_any, path_included  # noqa: E402

# A job whose p95 sits under this runs in the cheap tier: it is worth paying on
# every push. Anything slower waits behind the cheap tier and the review gate.
DEFAULT_TIER_THRESHOLD_S = 300

# --- what kind of answer a job gives -----------------------------------------
# `tier` says what a job costs. It does not say what its result *means*, and for
# one family of jobs the difference decides whether waiting for it is worth
# anything at all.
#
# A test returns a verdict: this code is correct, or it is not. A benchmark or a
# simulation returns a measurement: this code took 4.2s, this loop converged in
# 31 iterations. A verdict is why the merge waits. A measurement is not — it is
# a number to compare against yesterday's, and blocking a correctness fix until
# it arrives buys nothing, because nothing about the number says the fix is
# wrong. Worse, a measurement only means something when the diff could have
# moved it: run one on a README change and it costs the full wall clock to
# reproduce noise.
#
# So `kind` is a second axis, orthogonal to `tier`, and it is what decides
# whether a job is worth launching on this particular diff.
KIND_TEST = "test"
KIND_BENCHMARK = "benchmark"

# The vocabulary a repo uses when it means "measurement". Word-boundary anchored
# on purpose: `perf` as a substring matches `perform_migration_check`, and a
# migration check is a verdict. `sim` is deliberately absent for the same reason
# — it is a fragment of `similarity`, `simple` and `simulator-build`, and the
# cost of a wrong guess here is a correctness job stops being waited on.
BENCHMARK_VOCABULARY = re.compile(
    r"(?<![a-z0-9])(?:bench(?:mark)?(?:ing|s)?|perf|performance|simulation|simulate|soak"
    r"|load[-_ ]?test|stress[-_ ]?test|throughput|latency)(?![a-z0-9])",
    re.I,
)


class ProfileError(Exception):
    pass


DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc"}
DOC_DIRS = {"docs", "doc", "documentation"}
# Test sources only. The `.*` glob otherwise matches compiled bytecode, and a
# .pyc handed to a test runner is at best an error.
TEST_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".rb", ".go", ".rs", ".java", ".kt"}
SKIP_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".mypy_cache", ".ruff_cache"}
FINISHED = {"success", "failure"}


# --- workflow parsing ---------------------------------------------------------


def _on_block(doc: dict) -> dict:
    # YAML 1.1 reads a bare `on:` key as the boolean True. PyYAML obliges.
    raw = doc.get("on", doc.get(True, {}))
    if isinstance(raw, str):
        return {raw: {}}
    if isinstance(raw, list):
        return {k: {} for k in raw}
    return raw if isinstance(raw, dict) else {}


def _listed(value: object) -> list:
    """A filter value as the list Actions reads it as.

    `branches`, `tags`, `paths`, `types` and their `-ignore` forms take a
    list, and a bare scalar is read as a one-item list — the same shape `on:`
    and `needs:` take, and the same guard those two already had. Without it
    `list("docs/**")` was six single-character globs, one of them a bare `*`
    that matched every top-level file in the repository, and `branches: main`
    was four one-letter branch names. A mapping is not a filter at all, and
    reads as no filter: the conservative answer, since a job with no filter is
    one that fires on everything.
    """
    if value is None or isinstance(value, dict):
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _path_filters(on: dict, events: set[str] | None = None) -> list[str]:
    """Path filters declared by the given events (all of them when None).

    Filters belong to the event that declared them. A `paths:` on `push` says
    nothing about whether a job runs on a pull request, and treating the union as
    if it did marks unconditional PR jobs conditional.
    """
    paths: list[str] = []
    for event, cfg in on.items():
        if events is not None and str(event) not in events:
            continue
        if isinstance(cfg, dict):
            paths.extend(_listed(cfg.get("paths")))
    return sorted(dict.fromkeys(paths))


def parse_workflows(workflow_dir: Path, report_problems: bool = False):
    """Read .github/workflows into a flat list of jobs with their edges."""
    jobs: list[dict] = []
    problems: list[str] = []
    for path in sorted(Path(workflow_dir).glob("*.y*ml")):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{path.name}: unparseable ({type(exc).__name__})")
            continue
        if not isinstance(doc, dict):
            problems.append(f"{path.name}: not a workflow mapping")
            continue
        on = _on_block(doc)
        triggers = sorted(str(k) for k in on)
        filters = _path_filters(on)
        pr_filters = _path_filters(on, {"pull_request", "pull_request_target"})
        events = {
            str(event): {
                "paths": _listed((cfg or {}).get("paths")),
                "paths_ignore": _listed((cfg or {}).get("paths-ignore")),
                "branches": _listed((cfg or {}).get("branches")),
                "tags": _listed((cfg or {}).get("tags")),
                "types": _listed((cfg or {}).get("types")),
                "branches_ignore": _listed((cfg or {}).get("branches-ignore")),
                "tags_ignore": _listed((cfg or {}).get("tags-ignore")),
            }
            for event, cfg in on.items()
            if isinstance(cfg, dict) or cfg is None
        }
        for name, spec in (doc.get("jobs") or {}).items():
            spec = spec if isinstance(spec, dict) else {}
            needs = spec.get("needs", [])
            jobs.append(
                {
                    "name": name,
                    "display": spec.get("name"),
                    "workflow": doc.get("name", path.stem),
                    "workflow_file": path.name,
                    "needs": [needs] if isinstance(needs, str) else list(needs or []),
                    "triggers": triggers,
                    "path_filters": filters,
                    "pr_path_filters": pr_filters,
                    "events": events,
                }
            )
    return (jobs, problems) if report_problems else jobs


# --- observed cost ------------------------------------------------------------


def _seconds(job: dict) -> float | None:
    try:
        start = datetime.fromisoformat(job["started_at"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(job["completed_at"].replace("Z", "+00:00"))
    except (KeyError, ValueError, AttributeError):
        return None
    return (end - start).total_seconds()


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank. Predictable on the small samples CI history actually gives."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


_MATRIX_SUFFIX = re.compile(r"\s*\([^()]*\)\s*$")
_EXPRESSION = re.compile(r"\$\{\{.*?\}\}")


def attribute(reported: str, jobs: list[dict]) -> str | None:
    """Resolve a name GitHub reported back to the workflow job that declared it.

    Returns None rather than guessing. A wrong attribution silently mixes two
    jobs' durations, which is worse than an honest gap in the profile.
    """
    if not reported:
        return None
    candidates = {}
    for job in jobs:
        candidates[job["name"]] = job["name"]
        display = job.get("display")
        # A display name built from a matrix expression cannot be reversed.
        if display and not _EXPRESSION.search(display):
            candidates[display] = job["name"]
    # A reusable workflow reports as "caller / called-job"; the caller is the job
    # the workflow file declares.
    probes = [reported, _MATRIX_SUFFIX.sub("", reported)]
    if " / " in reported:
        head = reported.split(" / ")[0].strip()
        probes += [head, _MATRIX_SUFFIX.sub("", head)]
    for probe_name in probes:
        if probe_name in candidates:
            return candidates[probe_name]
    return None


def attribute_runs(job_runs: list[dict], jobs: list[dict]) -> tuple[list[dict], list[str]]:
    """Relabel observed runs with their declared job key; report what did not match."""
    mapped, orphans = [], set()
    for run in job_runs:
        key = attribute(run.get("name", ""), jobs)
        if key is None:
            orphans.add(run.get("name", ""))
        else:
            mapped.append({**run, "name": key})
    return mapped, sorted(n for n in orphans if n)


def duration_stats(job_runs: list[dict]) -> dict[str, dict]:
    buckets: dict[str, list[float]] = {}
    for job in job_runs:
        if job.get("conclusion") not in FINISHED:
            continue  # cancelled/skipped runs say nothing about what a job costs
        secs = _seconds(job)
        if secs is not None:
            buckets.setdefault(job["name"], []).append(secs)
    return {
        name: {"n": len(vals), "p50": _percentile(vals, 50), "p95": _percentile(vals, 95)}
        for name, vals in buckets.items()
    }


def classify_tiers(
    stats: dict[str, dict], threshold_s: int = DEFAULT_TIER_THRESHOLD_S
) -> dict[str, str]:
    return {
        name: ("cheap" if s.get("p95", 0) <= threshold_s else "expensive")
        for name, s in stats.items()
    }


def classify_kinds(
    jobs: list[dict], declared: list[str] | None = None
) -> dict[str, tuple[str, str]]:
    """Name -> (kind, why we say so), for every job in the workflow graph.

    Two sources, and which one answered is recorded, because the two carry very
    different weight downstream.

    `config` — `benchmark_jobs` in .foreman/config.json, matched as GitHub filter
    patterns against the job key, its display name and its workflow name. A human
    wrote it down; it is a fact about this repo, and `land.py` will stop waiting
    on a job on the strength of it.

    `name` — the job calls itself a benchmark. That is a guess, and a guess may
    never be the reason a merge stops waiting for a check: a job named
    `perf-regression-test` that really does gate the merge would be waved through
    on nothing but its name. So a name match only ever *narrows* what is spent
    where being wrong is harmless — it keeps benchmarks off the laptop, and it
    keeps them from being launched on a diff that cannot move their numbers.
    Whether a check gates the merge stays with branch protection.
    """
    patterns = list(declared or [])
    kinds: dict[str, tuple[str, str]] = {}
    for job in jobs:
        name = job["name"]
        # Every name this job answers to. A repo that hides `bench` behind
        # `name: Nightly` is caught by the workflow, and one whose workflow is
        # `CI` is caught by the job key.
        aliases = [name, job.get("display") or "", job.get("workflow") or ""]
        if any(matches_any(alias, patterns) for alias in aliases if alias):
            kinds[name] = (KIND_BENCHMARK, "config")
        elif any(BENCHMARK_VOCABULARY.search(alias) for alias in aliases if alias):
            kinds[name] = (KIND_BENCHMARK, "name")
        else:
            kinds[name] = (KIND_TEST, "default")
    return kinds


def flake_rates(job_runs: list[dict]) -> dict[str, float]:
    """A flake is one commit where the same job both failed and passed.

    Failing every time on a commit is a real failure, not a flake. That
    distinction is the whole point: it decides rerun versus fix.
    """
    by_job: dict[str, dict[str, set[str]]] = {}
    for job in job_runs:
        if job.get("conclusion") not in FINISHED:
            continue
        by_job.setdefault(job["name"], {}).setdefault(job.get("head_sha", ""), set()).add(
            job["conclusion"]
        )
    rates = {}
    for name, shas in by_job.items():
        flaky = sum(1 for outcomes in shas.values() if outcomes >= {"success", "failure"})
        rates[name] = flaky / len(shas) if shas else 0.0
    return rates


def required_checks(protection: dict | None) -> list[str]:
    """Only required checks can block a merge; everything else is advisory."""
    block = (protection or {}).get("required_status_checks") or {}
    names = list(block.get("contexts") or [])
    names += [c.get("context") for c in (block.get("checks") or []) if c.get("context")]
    return sorted(dict.fromkeys(names))


# --- test impact --------------------------------------------------------------


def _is_test_source(path: Path) -> bool:
    """A real test file, not a build artefact that happens to sit beside one."""
    return path.is_file() and path.suffix in TEST_SUFFIXES and not SKIP_DIRS & set(path.parts)


def _is_test(rel: str) -> bool:
    path = Path(rel)
    parts = path.parts
    return (
        bool(parts)
        and parts[0] in {"test", "tests"}
        and path.name.startswith("test")
        and path.suffix in TEST_SUFFIXES
        and not SKIP_DIRS & set(parts)
    )


def _is_doc(rel: str) -> bool:
    path = Path(rel)
    return path.suffix.lower() in DOC_SUFFIXES or bool(set(path.parts) & DOC_DIRS)


def impacted_tests(changed: list[str], repo_root: Path) -> tuple[list[str], bool]:
    """Map a diff to the tests that cover it.

    Returns (tests, complete). `complete` is False when any changed file could
    not be mapped — the caller must then run the full suite. Narrowing on a
    partial map is how you ship a regression, so this never guesses.
    """
    root = Path(repo_root)
    hits: set[str] = set()
    complete = True
    for rel in changed:
        if _is_test(rel):
            hits.add(rel)
        elif _is_doc(rel):
            continue  # documentation genuinely maps to no tests
        else:
            stem = Path(rel).stem
            found = [
                str(p.relative_to(root))
                for p in sorted(root.glob(f"tests/**/test_{stem}.*"))
                if _is_test_source(p)
            ]
            if found:
                hits.update(found)
            else:
                complete = False
    return sorted(hits), complete


# --- does this diff warrant a benchmark run? ----------------------------------


def _pr_config(spec: dict) -> dict | None:
    """The pull_request trigger's merged filter config, or None if it has none.

    `pull_request_target` counts too: it produces a check on the pull request in
    exactly the same way, and a repo that uses it for its benchmarks (the usual
    reason being a fork that needs secrets) declares its paths there.
    """
    events = spec.get("events") or {}
    for name in ("pull_request", "pull_request_target"):
        cfg = events.get(name)
        if isinstance(cfg, dict):
            return cfg
    return None


def _selected_by(cfg: dict, changed: list[str]) -> tuple[bool | None, list[str]]:
    """GitHub's own answer to "does this diff run this job?", and what decided it.

    Returns (runs, the changed files that decided it), or (None, []) when the
    trigger declares no path filter and so cannot answer. `paths` runs the job
    when ANY changed file matches; `paths-ignore` runs it when any changed file
    does NOT match. Both are any-tests over the diff, not all-tests — a
    one-file-matched diff runs the job, which is why an empty diff runs nothing.
    """
    paths = list(cfg.get("paths") or [])
    ignore = list(cfg.get("paths_ignore") or cfg.get("paths-ignore") or [])
    if not paths and not ignore:
        return None, []
    if paths:
        hits = [f for f in changed if path_included(f, paths)]
        if hits or not ignore:
            return bool(hits), hits
    # `paths-ignore` alone, or a `paths` list nothing matched while an ignore
    # list also exists: what runs the job is a file the ignore list does not
    # cover. `path_included` is still the right test — an ignore list may itself
    # carry `!` re-inclusions — so the ignored set is what it includes, and the
    # deciding files are the rest.
    hits = [f for f in changed if not path_included(f, ignore)]
    return bool(hits), hits


def _configured_paths(benchmark_paths: object, job: str) -> list[str]:
    """`benchmark_paths` as globs for one job: a flat list covers every job."""
    if isinstance(benchmark_paths, dict):
        return list(benchmark_paths.get(job) or [])
    if isinstance(benchmark_paths, (list, tuple)):
        return list(benchmark_paths)
    return []


def _decision(job: str, spec: dict, **fields) -> dict:
    """One job's answer, always carrying where the job is declared.

    The workflow file is what makes the resulting issue actionable: "declare what
    `bench` covers" is a sentence somebody has to turn into a `paths:` filter, and
    they need to be told which of thirty workflow files to open.
    """
    return {"job": job, "workflow_file": spec.get("workflow_file"), **fields}


def warrants_benchmark(
    job: str, spec: dict, changed: list[str], benchmark_paths: object = None
) -> dict:
    """Whether this diff can move this benchmark's numbers.

    Three answers, not two, and the third is the one that matters.

    `true`  — something the benchmark covers changed. Worth measuring.
    `false` — nothing it covers changed. A run here reproduces noise at full
              wall-clock cost, which is the spend this whole decision exists to
              stop.
    `null`  — nothing in the repo says what this benchmark covers, so neither
              answer is honest. It is reported as a gap rather than guessed at,
              because both guesses are bad in a way that never surfaces:
              guessing `true` restores the every-PR spend, and guessing `false`
              silently stops measuring a benchmark nobody will notice went quiet.
              A gap becomes work (see `benchmark_findings`), and once someone
              declares the paths, every future diff gets a real answer.

    The ladder is ordered by who is more likely to be right. A `paths:` filter on
    the job's own trigger is GitHub's answer, already agreed by the team and
    already enforced on every push — deciding differently here would put the loop
    at odds with what CI actually does.
    """
    cfg = _pr_config(spec)
    if cfg is not None:
        runs, hits = _selected_by(cfg, changed)
        if runs is not None:
            return _decision(job, spec, warranted=runs, basis="workflow path filter", matched=hits)

    configured = _configured_paths(benchmark_paths, job)
    if configured:
        # Written to look like a workflow `paths:` filter because that is what it
        # stands in for, so it reads by the same rules — `!` included.
        hits = [f for f in changed if path_included(f, configured)]
        return _decision(
            job, spec, warranted=bool(hits), basis="configured benchmark_paths", matched=hits
        )

    if changed and all(_is_doc(f) for f in changed):
        # The one thing that can be said without any declaration at all. Prose
        # has no runtime, so no benchmark reading can move because of it.
        return _decision(job, spec, warranted=False, basis="documentation-only diff", matched=[])

    return _decision(
        job,
        spec,
        warranted=None,
        basis="nothing declares what this benchmark covers",
        matched=[],
    )


def benchmark_plan(profile: dict, changed: list[str], config: dict | None = None) -> dict:
    """What to measure on this diff, and what to leave unmeasured.

    `run` is the only field that spends anything, and it is deliberately narrow:
    a job is launched when something says it should be, never merely because
    nothing said it should not. That asymmetry is the saving.
    """
    config = config or {}
    changed = list(changed or [])
    jobs = profile.get("jobs") or {}
    benchmark_paths = config.get("benchmark_paths")

    decisions = [
        warrants_benchmark(name, spec, changed, benchmark_paths)
        for name, spec in sorted(jobs.items())
        if (spec or {}).get("kind") == KIND_BENCHMARK
    ]
    run = [d["job"] for d in decisions if d["warranted"] is True]
    skip = [d["job"] for d in decisions if d["warranted"] is False]
    unknown = [d["job"] for d in decisions if d["warranted"] is None]

    saved = sum((jobs.get(name) or {}).get("p95") or 0 for name in skip + unknown)
    return {
        "decisions": decisions,
        "run": run,
        "skip": skip,
        "unknown": unknown,
        "seconds_not_spent": saved,
        "recommendation": _benchmark_recommendation(run, unknown),
    }


def _benchmark_recommendation(run: list[str], unknown: list[str]) -> str:
    if run:
        return (
            f"this diff can move {', '.join(run)} — measure it, but as its own effort: "
            "a measurement is not a verdict, so it must not sit in this batch's merge path"
        )
    if unknown:
        return (
            f"nothing declares what {', '.join(unknown)} covers, so nothing is launched; "
            "declare it once in benchmark_paths and every future diff gets a real answer"
        )
    return "no benchmark on this repo can be moved by this diff"


BENCHMARK_LABEL_HINT = "enhancement"


def benchmark_findings(plan: dict, batch: str | None = None) -> list[dict]:
    """The plan as findings, so `findings.py` can file them without a second filer.

    This is the "parallel effort" half of the decision. A benchmark that this
    diff warrants must still not gate the batch — the batch is a correctness fix
    and the benchmark is a number — so the run does not go into the batch's PR.
    It goes here, becomes an issue, and triage picks it up as work of its own,
    which is how it ends up in its own pull request running beside the fix
    instead of in front of it.

    A gap gets an issue for the same reason: it is a one-line declaration that
    permanently improves every later decision, and nothing else in the loop will
    ever produce it.
    """
    where = f"batch `{batch}`" if batch else "this diff"
    decisions = plan.get("decisions") or []
    findings = []

    for decision in decisions:
        if decision["warranted"] is not True:
            continue
        job = decision["job"]
        workflow = decision.get("workflow_file")
        matched = ", ".join(f"`{m}`" for m in decision["matched"][:5]) or "the diff"
        findings.append(
            {
                "summary": f"Run the {job} benchmark against {where}: it changed {matched}",
                "severity": "low",
                "file": f".github/workflows/{workflow}" if workflow else "unknown file",
                "failure_scenario": (
                    f"{where} changed code that {job} measures ({decision['basis']}), so its "
                    f"numbers may have moved. The batch merges on correctness signals and does "
                    f"not wait for this, which is why it needs an effort of its own: without "
                    f"one the change lands and the measurement is never taken."
                ),
            }
        )

    # One finding for every undeclared benchmark, not one each. Two of them
    # produce titles differing by a single word, which `findings.plan` reads as
    # duplicates and rightly so — but the survivor names one job, so the other
    # benchmark's gap would be closed by an issue that never mentions it, and
    # would never be raised again. The fix is the shape, not the threshold: this
    # is one edit to one file, so it is one issue.
    gaps = [d for d in decisions if d["warranted"] is None]
    if gaps:
        named = ", ".join(d["job"] for d in gaps)
        declared_in = sorted(
            {f".github/workflows/{d['workflow_file']}" for d in gaps if d.get("workflow_file")}
        )
        findings.append(
            {
                "summary": (f"Declare in benchmark_paths what these benchmarks cover: {named}"),
                "severity": "low",
                # The config is where the answer goes. The workflows are named in
                # the body, since a `paths:` filter on the job itself is the
                # better fix wherever the job can carry one.
                "file": ".foreman/config.json",
                "failure_scenario": (
                    f"Nothing in the workflows or in .foreman/config.json says which paths "
                    f"{named} measure, so the loop cannot tell a diff that moves their numbers "
                    f"from one that cannot. It will not guess, so they are launched on no diff "
                    f"at all — benchmarks that have quietly stopped measuring anything. A "
                    f"`paths:` filter in {', '.join(declared_in) or 'the workflow'}, or a "
                    f"benchmark_paths entry per job, ends this permanently."
                ),
            }
        )
    return findings


# --- assembly -----------------------------------------------------------------


# Allow-lists narrow a trigger to what they list; ignore-lists only ever remove
# runs from it. The two directions compare oppositely, which is the whole reason
# a filter *count* cannot stand in for how permissive a trigger is.
_ALLOW_FILTERS = ("paths", "branches", "tags", "types")
_IGNORE_FILTERS = ("paths_ignore", "branches_ignore", "tags_ignore")
_FILTERS = _ALLOW_FILTERS + _IGNORE_FILTERS

# Activity types that occur while a pull request is still open. A trigger
# restricted to any other type — `types: [closed]` — cannot put a check on a live
# PR at all. This is deliberately broader than land.PR_UNCONDITIONAL_TYPES, which
# asks the stricter question of whether a job reports on *every* PR and so may be
# required; conflating the two was issue #49. Containment between them is asserted
# in tests/test_ci_profile.py.
OPEN_PR_TYPES = frozenset({"opened", "synchronize", "reopened", "ready_for_review", "edited"})


def _reports_while_open(cfg: dict) -> bool:
    types = set(cfg.get("types") or [])
    return not types or bool(types & OPEN_PR_TYPES)


def _fires_wherever(a: dict, b: dict) -> bool:
    """Whether everything that makes `b` fire also makes `a` fire.

    Per axis, because the filters are a conjunction: an absent allow-list is no
    restriction at all, a present one has to cover b's, and an ignore list is
    weaker the shorter it is. `types` is the one approximation — an absent
    `types` means the default activity types rather than every type — and it errs
    towards keeping the declaration that reports on an open pull request, which
    is the only situation the gate ever asks about.
    """
    for key in _ALLOW_FILTERS:
        mine, theirs = set(a.get(key) or []), set(b.get(key) or [])
        if mine and not (theirs and mine >= theirs):
            return False
    for key in _IGNORE_FILTERS:
        mine, theirs = set(a.get(key) or []), set(b.get(key) or [])
        if mine and not mine <= theirs:
            return False
    return True


def _canonical(cfg: dict) -> str:
    return json.dumps({key: sorted(set(cfg.get(key) or [])) for key in _FILTERS}, sort_keys=True)


def _informativeness(cfg: dict) -> tuple:
    """Ranks two declarations that neither cover each other nor combine, best first.

    Every choice here is sound: one real declaration always fires on a subset of
    what the job as a whole fires on, and under-requiring costs nothing — a job
    that does report lands in `actionable_pending` while it runs. So this ranks
    by how much of the job's behaviour survives the compression. A declaration
    that cannot report while the PR is open says nothing whatsoever; after that,
    a `branches` filter is still resolved against the PR's base at query time,
    while a path or tag filter can never be. The serialised config breaks the
    remaining ties so the answer never depends on which file sorted first.
    """
    unresolvable = any(cfg.get(key) for key in ("paths", "paths_ignore", "tags", "tags_ignore"))
    return (not _reports_while_open(cfg), unresolvable, _canonical(cfg))


def _merge_triggers(declarations: list[dict]) -> dict:
    """Fold every declaration of one job name on one event into a single config.

    A check appears if ANY declaration fires, so the merged config should stand
    for the union of their firing sets. One filter dict cannot express a
    disjunction that crosses two axes — `branches: [main]` OR `paths: [src/**]`
    is emphatically not "no filters", and merging key by key would produce
    exactly that, marking a job requirable that GitHub may never run. So the
    union is taken only where it is exact, and otherwise one declaration is kept.
    """
    merged, *rest = sorted(declarations, key=_canonical)  # input order cannot decide
    for cfg in rest:
        if _fires_wherever(merged, cfg):
            continue
        if _fires_wherever(cfg, merged):
            merged = cfg
            continue
        differing = [k for k in _FILTERS if set(merged.get(k) or []) != set(cfg.get(k) or [])]
        if len(differing) == 1 and differing[0] in _ALLOW_FILTERS:
            # Every other axis is identical, so the disjunction is precisely the
            # union of the two lists on this one.
            key = differing[0]
            merged = {**merged, key: sorted(set(merged.get(key) or []) | set(cfg.get(key) or []))}
            continue
        merged = min((merged, cfg), key=_informativeness)
    return merged


def build_profile(
    workflow_dir: Path,
    job_runs: list[dict],
    protection: dict | None,
    threshold_s: int = DEFAULT_TIER_THRESHOLD_S,
    benchmark_jobs: list[str] | None = None,
) -> dict:
    jobs, problems = parse_workflows(workflow_dir, report_problems=True)
    attributed, unattributed = attribute_runs(job_runs, jobs)
    stats = duration_stats(attributed)
    tiers = classify_tiers(stats, threshold_s)
    flakes = flake_rates(attributed)
    required = set(required_checks(protection))

    # A job name reused across workflows is one check name, so merge rather than
    # overwrite: a release-only `test` job must not erase the PR `test` job.
    by_name: dict[str, dict] = {}
    declared: dict[str, dict[str, list[dict]]] = {}
    for job in jobs:
        for event, cfg in (job.get("events") or {}).items():
            declared.setdefault(job["name"], {}).setdefault(event, []).append(cfg)
        seen = by_name.get(job["name"])
        if seen is None:
            by_name[job["name"]] = dict(job)
            continue
        seen["triggers"] = sorted(set(seen["triggers"]) | set(job["triggers"]))
        seen["path_filters"] = sorted(set(seen["path_filters"]) | set(job["path_filters"]))
        seen["pr_path_filters"] = sorted(set(seen["pr_path_filters"]) | set(job["pr_path_filters"]))
        # The graph edges too: a `test` that needs `lint` in one workflow and
        # nothing in another still needs `lint` to be `test`, and which file
        # sorted first must not decide whether the profile says so.
        seen["needs"] = sorted(set(seen["needs"]) | set(job["needs"]))
    # Every declaration of an event is collected before any of them is merged, so
    # the result depends on what the workflows say and not on the order the files
    # happened to be read in.
    for name, events in declared.items():
        by_name[name]["events"] = {event: _merge_triggers(cfgs) for event, cfgs in events.items()}
    jobs = list(by_name.values())
    # After the merge, not before: a job name declared in two workflows is one
    # check, and `benchmark_jobs: ["nightly"]` matching either declaration has to
    # mark the single job it collapses into.
    kinds = classify_kinds(jobs, benchmark_jobs)

    # Branch protection stores required checks as the names GitHub *reports*, not
    # as the keys the workflow declares. A matrix job `test` reports one context
    # per cell — `test (3.11)`, `test (3.12)` — and a job with a `name:` reports
    # under that display name, so matching the key against the context list said
    # "not required" about the very jobs that gate the merge. `attribute` reverses
    # a reported name back to the job that declared it, and returns None rather
    # than guessing, so a third-party context like `codecov/patch` marks nothing.
    #
    # Partial coverage rounds up: one required cell means this job can block a
    # merge, which is the only question the flag answers. The per-cell truth is
    # not lost — `required_checks` still lists the exact contexts protection
    # named. Rounding down would let a failing required cell read as advisory,
    # turning a red gate green; rounding up at worst costs a wait.
    required_jobs = set()
    for context in required:
        key = attribute(context, jobs)
        if key is not None:
            required_jobs.add(key)

    merged, unmeasured = {}, []
    for job in jobs:
        name = job["name"]
        stat = stats.get(name)
        if stat is None:
            unmeasured.append(name)
        merged[name] = {
            **{
                k: job[k]
                for k in (
                    "workflow",
                    "workflow_file",
                    "needs",
                    "triggers",
                    "path_filters",
                    "pr_path_filters",
                    "events",
                )
            },
            "display": job.get("display"),
            "p50": stat["p50"] if stat else None,
            "p95": stat["p95"] if stat else None,
            "samples": stat["n"] if stat else 0,
            "tier": tiers.get(name, "unmeasured"),
            "kind": kinds.get(name, (KIND_TEST, "default"))[0],
            "kind_source": kinds.get(name, (KIND_TEST, "default"))[1],
            "required": name in required_jobs,
            "flake_rate": round(flakes.get(name, 0.0), 3),
        }

    def tier_cost(tier: str) -> float:
        return sum(j["p95"] or 0 for j in merged.values() if j["tier"] == tier)

    # Counted separately from the tiers rather than carved out of them, because
    # the two questions are independent: a benchmark is usually expensive but a
    # 40-second smoke simulation is cheap, and it is still not a verdict. The
    # tier totals stay the honest answer to "what does a full run cost"; this is
    # the answer to "how much of that is measurement the loop need not wait for".
    benchmarks = sorted(n for n, j in merged.items() if j["kind"] == KIND_BENCHMARK)

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "tier_threshold_s": threshold_s,
        "jobs": merged,
        "required_checks": sorted(required),
        # Absent protection means we do not know what is required. Recording
        # that as a fact stops land.py reading it as "nothing is".
        "protection_known": bool(protection),
        "cheap_tier_s": tier_cost("cheap"),
        "expensive_tier_s": tier_cost("expensive"),
        "benchmark_jobs": benchmarks,
        "benchmark_s": sum(merged[n]["p95"] or 0 for n in benchmarks),
        "unmeasured_jobs": sorted(unmeasured),
        "unattributed_runs": unattributed,
        "problems": problems,
    }


# --- live probe ---------------------------------------------------------------


def _gh_json(args: list[str]) -> object:
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
        return json.loads(out) if out.strip() else None
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None  # no gh on PATH is one more way to have no answer


def current_repo() -> str | None:
    """The repo the cwd belongs to, or None outside a GitHub checkout."""
    info = _gh_json(["repo", "view", "--json", "nameWithOwner"]) or {}
    return info.get("nameWithOwner")


def _fetch_job_runs(repo: str, runs: int, branch: str | None) -> list[dict]:
    listing = (
        _gh_json(
            [
                "run",
                "list",
                "--repo",
                repo,
                "--limit",
                str(runs),
                *(["--branch", branch] if branch else []),
                "--json",
                "databaseId,headSha,conclusion",
            ]
        )
        or []
    )
    job_runs = []
    for run in listing:
        detail = _gh_json(["api", f"repos/{repo}/actions/runs/{run['databaseId']}/jobs"]) or {}
        for job in detail.get("jobs", []):
            job_runs.append(
                {
                    "run_id": run["databaseId"],
                    "head_sha": run.get("headSha"),
                    "name": job.get("name"),
                    "conclusion": job.get("conclusion"),
                    "started_at": job.get("started_at"),
                    "completed_at": job.get("completed_at"),
                }
            )
    return job_runs


def _fetch_protection(repo: str) -> dict | None:
    view = _gh_json(["repo", "view", repo, "--json", "defaultBranchRef"]) or {}
    branch = (view.get("defaultBranchRef") or {}).get("name", "main")
    # Absent or inaccessible protection is normal, not an error — but it means
    # UNKNOWN, not "nothing is required". build_profile records that as
    # protection_known, and land.py then treats every check as required.
    return _gh_json(["api", f"repos/{repo}/branches/{branch}/protection"])


def declared_benchmarks(config_path: str | None = None) -> list[str]:
    """`benchmark_jobs` from the config, read quietly.

    `ledger.load_config` warns when the file is missing, and rightly — running
    without caps is dangerous. Missing this key is not: it costs a name-matched
    guess instead of a declaration, which is the documented fallback. Probing a
    repo should not print a scare about brakes it is not touching.
    """
    resolved = ledger.resolve_config(config_path)
    if not resolved.is_file():
        return []
    try:
        loaded = json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []  # load_config raises on this where it matters; here it is a hint
    value = loaded.get("benchmark_jobs")
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else []


def _quiet_config(config_path: str | None = None) -> dict:
    """The whole config, read without the missing-file warning. See above."""
    resolved = ledger.resolve_config(config_path)
    if not resolved.is_file():
        return {}
    try:
        return json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def probe(
    repo: str,
    runs: int = 50,
    branch: str | None = None,
    workflow_dir: Path | None = None,
    threshold_s: int = DEFAULT_TIER_THRESHOLD_S,
    benchmark_jobs: list[str] | None = None,
) -> dict:
    """Pull real run history off GitHub. Read-only: run list, jobs, protection.

    Job *history* comes from `repo`; job *definitions* come from workflow files
    on disk. Reading those from different repos yields a profile describing
    neither, so probing a repo other than the current one requires saying where
    its workflows are.
    """
    if workflow_dir is None:
        here = current_repo()
        if here and here.lower() != repo.lower():
            raise ProfileError(
                f"refusing to profile {repo} from a checkout of {here}: run history would "
                f"come from {repo} but job definitions from {here}. Run this inside {repo}, "
                f"or pass --workflows pointing at its workflow directory."
            )
        workflow_dir = Path(".github/workflows")

    workflow_dir = Path(workflow_dir)
    if not workflow_dir.is_dir():
        raise ProfileError(f"no workflow directory at {workflow_dir}: nothing to profile")

    profile = build_profile(
        workflow_dir,
        _fetch_job_runs(repo, runs, branch),
        _fetch_protection(repo),
        threshold_s,
        benchmark_jobs,
    )
    profile["repo"] = repo
    return profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--repo", required=True)
    p.add_argument("--runs", type=int, default=50)
    p.add_argument(
        "--out",
        help=f"where to write the profile (default {ledger.LEDGER_DIR}/{ledger.PROFILE_FILE} "
        "in the repository root)",
    )
    p.add_argument(
        "--workflows",
        default=None,
        help="workflow directory (required when profiling another repo)",
    )
    p.add_argument("--branch", default=None)
    p.add_argument("--threshold", type=int, default=DEFAULT_TIER_THRESHOLD_S)
    p = sub.add_parser("impact")
    p.add_argument("--changed", nargs="+", required=True)
    p.add_argument("--root", default=".")
    p = sub.add_parser(
        "benchmark-plan",
        help="decide which benchmark or simulation jobs this diff can move",
    )
    p.add_argument("--changed", nargs="+", required=True)
    p.add_argument("--profile", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--batch", default=None, help="named in the findings, for provenance")

    args = parser.parse_args(argv)
    if args.cmd == "probe":
        try:
            profile = probe(
                args.repo,
                args.runs,
                args.branch,
                Path(args.workflows) if args.workflows else None,
                args.threshold,
                declared_benchmarks(),
            )
        except ProfileError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        # Written where every reader looks: the repository's `.foreman`, not the
        # caller's. Run from a build worktree, the default used to create a
        # second `.foreman` there and the profile every reader anchors to
        # stayed missing (issue #74).
        out = ledger.resolve_profile(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(profile, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "written": str(out),
                    "jobs": len(profile["jobs"]),
                    "cheap_tier_s": profile["cheap_tier_s"],
                    "expensive_tier_s": profile["expensive_tier_s"],
                    "benchmark_jobs": profile["benchmark_jobs"],
                    "unmeasured": profile["unmeasured_jobs"],
                },
                indent=2,
            )
        )
    elif args.cmd == "benchmark-plan":
        profile = ledger.load_profile(args.profile)
        if not profile.get("jobs"):
            # No profile means no `kind` on anything, so this cannot answer. Say
            # so rather than returning an empty plan, which reads identically to
            # "this repo has no benchmarks" and would quietly excuse every one.
            print(
                "warning: no CI profile, so no job is known to be a benchmark; "
                "run `ci_profile.py probe` before trusting an empty plan",
                file=sys.stderr,
            )
        config = ledger.load_config(args.config) if args.config else _quiet_config()
        plan = benchmark_plan(profile, args.changed, config)
        print(
            json.dumps(
                {**plan, "findings": benchmark_findings(plan, args.batch)},
                indent=2,
            )
        )
    else:
        tests, complete = impacted_tests(args.changed, Path(args.root))
        print(
            json.dumps(
                {
                    "tests": tests,
                    "complete": complete,
                    "recommendation": "run listed tests" if complete else "run full suite",
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
